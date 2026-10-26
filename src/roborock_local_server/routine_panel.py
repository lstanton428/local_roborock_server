"""Routines for the Home Assistant ingress panel.

Lists, runs and edits the vacuum's routines (scenes) in the server's inventory
through the same scene services the Roborock app's requests use, so routines
saved here look and run like ones saved in the app.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from . import backend as _backend  # noqa: F401  (puts the bundled backend on sys.path)
from https_server.routes.user.homes.service import normalize_rooms
from https_server.routes.user.scene import service as scene_service
from shared.context import ServerContext
from shared.data_helpers import as_int, get_value
from shared.inventory_io import load_inventory
from shared.routine_editor import (
    FAN_POWERS,
    MOP_MODES,
    WATER_BOX_MODES,
    build_scene_param,
    editable_routine,
    parse_routine_draft,
)
from shared.routine_runner import RoutineExecutionError, routine_device_record
from shared.vacuum_map import read_vacuum_map

LOG = logging.getLogger(__name__)


class RoutineNotFound(LookupError):
    """The routine or vacuum is not in the inventory."""


class RoutinePanel:
    """What the ingress panel may do with routines; no keys or sessions leave it."""

    def __init__(self, context: ServerContext) -> None:
        self._context = context
        self._map_lock = asyncio.Lock()

    def _inventory(self) -> dict[str, Any]:
        return load_inventory(self._context)

    def vacuums(self) -> list[dict[str, str]]:
        inventory = self._inventory()
        vacuums: list[dict[str, str]] = []
        seen: set[str] = set()
        for key in ("devices", "received_devices", "receivedDevices"):
            devices = inventory.get(key)
            for device in devices if isinstance(devices, list) else []:
                if not isinstance(device, dict):
                    continue
                duid = str(get_value(device, "duid", "device_id", "deviceId", default="")).strip()
                if not duid or duid in seen:
                    continue
                seen.add(duid)
                name = str(get_value(device, "name", "device_name", default="")).strip() or duid
                vacuums.append({"duid": duid, "name": name})
        return vacuums

    def _scenes(self) -> list[dict[str, Any]]:
        scenes = self._inventory().get("scenes")
        return [dict(scene) for scene in scenes if isinstance(scene, dict)] if isinstance(scenes, list) else []

    def _scene(self, scene_id: int) -> dict[str, Any]:
        for scene in self._scenes():
            if as_int(get_value(scene, "id", default=0), 0) == scene_id:
                return scene
        raise RoutineNotFound(f"Routine {scene_id} not found")

    def _require_vacuum(self, device_id: str) -> None:
        if device_id not in {vacuum["duid"] for vacuum in self.vacuums()}:
            raise RoutineNotFound(f"Vacuum {device_id} not found")

    def list_routines(self) -> dict[str, Any]:
        names = {vacuum["duid"]: vacuum["name"] for vacuum in self.vacuums()}
        scenes = {as_int(get_value(scene, "id", default=0), 0): scene for scene in self._scenes()}
        routines = []
        for scene_id in scene_service.scene_order(self._context, {}):
            scene = scenes.get(scene_id)
            if scene is None:
                continue
            routine = editable_routine(scene)
            routine["device_name"] = names.get(routine["device_id"], routine["device_id"])
            routines.append(routine)
        return {
            "vacuums": self.vacuums(),
            "routines": routines,
            "options": {
                "fan_power": FAN_POWERS,
                "water_box_mode": WATER_BOX_MODES,
                "mop_mode": MOP_MODES,
            },
        }

    def routine(self, scene_id: int) -> dict[str, Any]:
        return editable_routine(self._scene(scene_id))

    def run(self, scene_id: int) -> dict[str, Any]:
        """Start the routine, or stop it if it is already running (as the app does)."""
        self._scene(scene_id)
        return scene_service.execute_scene(self._context, scene_id)

    def create(self, body: Any) -> dict[str, Any]:
        device_id = str(body.get("device_id") or "").strip() if isinstance(body, dict) else ""
        self._require_vacuum(device_id)
        draft = parse_routine_draft(body)
        param = build_scene_param(device_id=device_id, draft=draft)
        request = {"name": draft.name, "deviceId": device_id, "param": json.dumps(param, separators=(",", ":"))}
        created = scene_service.create_scene(self._context, {"__json": [json.dumps(request)]})
        return editable_routine(self._scene(as_int(created.get("id"), 0)))

    def update(self, scene_id: int, body: Any) -> dict[str, Any]:
        scene = self._scene(scene_id)
        if not editable_routine(scene)["editable"]:
            raise ValueError("This routine has steps the editor cannot change")
        device_id = str(get_value(scene, "device_id", "deviceId", "duid", default="")).strip()
        draft = parse_routine_draft(body)
        existing = scene.get("param")
        existing_param = json.loads(existing) if isinstance(existing, str) and existing.strip() else None
        param = build_scene_param(device_id=device_id, draft=draft, existing_param=existing_param)
        request = {"deviceId": device_id, "param": json.dumps(param, separators=(",", ":"))}
        scene_service.update_scene_param(self._context, scene_id, {"__json": [json.dumps(request)]})
        if draft.name != str(scene.get("name") or ""):
            scene_service.update_scene_name(self._context, scene_id, {"name": [draft.name]})
        return editable_routine(self._scene(scene_id))

    def rename(self, scene_id: int, name: Any) -> dict[str, Any]:
        self._scene(scene_id)
        clean = " ".join(str(name or "").split()) if isinstance(name, str) else ""
        if not clean or len(clean) > 64:
            raise ValueError("name must be 1 to 64 characters")
        scene_service.update_scene_name(self._context, scene_id, {"name": [clean]})
        return editable_routine(self._scene(scene_id))

    def delete(self, scene_id: int) -> None:
        self._scene(scene_id)
        scene_service.delete_scene(self._context, scene_id)

    def room_names(self) -> dict[str, str]:
        return {str(room["id"]): str(room["name"]) for room in normalize_rooms(self._inventory())}

    async def vacuum_map(self, device_id: str) -> dict[str, Any]:
        """The vacuum's current map and rooms; only reads from the vacuum."""
        self._require_vacuum(device_id)
        device = routine_device_record(self._context, device_id)
        async with self._map_lock:
            vacuum_map = await read_vacuum_map(
                self._context,
                device,
                room_names=self.room_names(),
                logger=LOG,
            )
        image = base64.b64encode(vacuum_map.image_png).decode("ascii")
        return {**vacuum_map.as_dict(), "image": f"data:image/png;base64,{image}"}


__all__ = ["RoutineExecutionError", "RoutineNotFound", "RoutinePanel"]
