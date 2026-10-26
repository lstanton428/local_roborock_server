"""Build and read routine (scene) params for the Home Assistant panel's editor.

Routines are saved in the param format the Roborock app uses, so the routine
runner, Home Assistant's Roborock integration and other apps see and run them:
one CMD action item per step, each with a do_scenes_segments or
do_scenes_zones payload holding a single data entry with its own tid.

Zones drawn in the editor carry their ``range`` in mm. The robot has never
stored them, so the runner cleans them with app_zoned_clean and nothing is
written to the robot.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import time
from typing import Any

from .routine_runner import RoutineExecutionError, parse_scene_steps

__all__ = [
    "FAN_POWERS",
    "MOP_MODES",
    "RoutineDraft",
    "RoutineStepDraft",
    "WATER_BOX_MODES",
    "build_scene_param",
    "editable_routine",
    "parse_routine_draft",
]

# Values the Saros and Qrevo families accept; labels follow the Roborock app.
FAN_POWERS = {101: "Quiet", 102: "Balanced", 103: "Turbo", 104: "Max", 108: "Max+"}
WATER_BOX_MODES = {200: "Off", 201: "Mild", 202: "Standard", 203: "Intense", 208: "Extreme"}
MOP_MODES = {300: "Standard", 301: "Deep", 303: "Deep+", 304: "Fast"}
# Values outside the lists above that existing routines may hold.
_FAN_POWER_RANGE = range(101, 111)
_WATER_BOX_MODE_RANGE = range(200, 210)
_MOP_MODE_RANGE = range(300, 307)

DEFAULT_FAN_POWER = 102
DEFAULT_WATER_BOX_MODE = 200
DEFAULT_MOP_MODE = 300

MAX_NAME_LENGTH = 64
MAX_STEPS = 20
MAX_ROOMS_PER_STEP = 32
MAX_ZONES_PER_STEP = 5
MAX_REPEAT = 3
# Robot coordinates are unsigned 16-bit mm.
_MAX_COORDINATE = 65535
_MIN_ZONE_SIDE_MM = 200

# The cloud's scene source marker; the app writes it into every step.
_SCENE_SOURCE = 101
_STEP_FINISHED_DP = 130
_STEP_METHODS = {"rooms": "do_scenes_segments", "zone": "do_scenes_zones"}


@dataclass(frozen=True)
class RoutineStepDraft:
    kind: str  # "rooms" or "zone"
    fan_power: int
    water_box_mode: int
    mop_mode: int
    repeat: int
    rooms: tuple[int, ...] = ()
    # [x1, y1, x2, y2] in mm, x1 < x2 and y1 < y2.
    zones: tuple[tuple[int, int, int, int], ...] = ()
    # Keep the zones of this existing step (zones the robot stores itself).
    keep_tid: str = ""
    name: str = ""


@dataclass(frozen=True)
class RoutineDraft:
    name: str
    steps: tuple[RoutineStepDraft, ...]


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be a number")
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a number") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be a number")
    return int(round(number))


def _setting(raw: Mapping[str, Any], field: str, default: int, allowed: range) -> int:
    value = raw.get(field, default)
    number = _as_int(value, field)
    if number not in allowed:
        raise ValueError(f"{field} {number} is not a supported value")
    return number


def _name(value: Any, *, field: str, required: bool) -> str:
    name = " ".join(str(value or "").split())
    if required and not name:
        raise ValueError(f"{field} is required")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{field} is longer than {MAX_NAME_LENGTH} characters")
    return name


def _zone(raw: Any, index: int) -> tuple[int, int, int, int]:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"zone {index} must be [x1, y1, x2, y2]")
    x1, y1, x2, y2 = (_as_int(value, f"zone {index}") for value in raw)
    left, right = sorted((x1, x2))
    bottom, top = sorted((y1, y2))
    if left < 0 or bottom < 0 or right > _MAX_COORDINATE or top > _MAX_COORDINATE:
        raise ValueError(f"zone {index} is outside the map")
    if right - left < _MIN_ZONE_SIDE_MM or top - bottom < _MIN_ZONE_SIDE_MM:
        raise ValueError(f"zone {index} is smaller than {_MIN_ZONE_SIDE_MM} mm")
    return left, bottom, right, top


def _step(raw: Any, index: int) -> RoutineStepDraft:
    if not isinstance(raw, dict):
        raise ValueError(f"step {index} must be an object")
    kind = raw.get("kind")
    if kind not in _STEP_METHODS:
        raise ValueError(f"step {index} kind must be rooms or zone")
    settings = {
        "fan_power": _setting(raw, "fan_power", DEFAULT_FAN_POWER, _FAN_POWER_RANGE),
        "water_box_mode": _setting(raw, "water_box_mode", DEFAULT_WATER_BOX_MODE, _WATER_BOX_MODE_RANGE),
        "mop_mode": _setting(raw, "mop_mode", DEFAULT_MOP_MODE, _MOP_MODE_RANGE),
        "repeat": _setting(raw, "repeat", 1, range(1, MAX_REPEAT + 1)),
    }
    name = _name(raw.get("name"), field=f"step {index} name", required=False)
    if kind == "rooms":
        rooms_raw = raw.get("rooms")
        if not isinstance(rooms_raw, list) or not rooms_raw:
            raise ValueError(f"step {index} needs at least one room")
        if len(rooms_raw) > MAX_ROOMS_PER_STEP:
            raise ValueError(f"step {index} has too many rooms")
        rooms = tuple(_as_int(value, f"step {index} room") for value in rooms_raw)
        if any(room <= 0 for room in rooms) or len(set(rooms)) != len(rooms):
            raise ValueError(f"step {index} rooms must be distinct segment ids")
        return RoutineStepDraft(kind=kind, rooms=rooms, name=name, **settings)

    keep_tid = str(raw.get("keep_tid") or "").strip()
    if keep_tid:
        if not keep_tid.isdigit():
            raise ValueError(f"step {index} keep_tid is invalid")
        return RoutineStepDraft(kind=kind, keep_tid=keep_tid, name=name, **settings)
    zones_raw = raw.get("zones")
    if not isinstance(zones_raw, list) or not zones_raw:
        raise ValueError(f"step {index} needs at least one zone")
    if len(zones_raw) > MAX_ZONES_PER_STEP:
        raise ValueError(f"step {index} has more than {MAX_ZONES_PER_STEP} zones")
    zones = tuple(_zone(zone, number) for number, zone in enumerate(zones_raw, start=1))
    return RoutineStepDraft(kind=kind, zones=zones, name=name, **settings)


def parse_routine_draft(body: Any) -> RoutineDraft:
    """Validate the editor's JSON; raises ValueError with a message for the panel."""
    if not isinstance(body, dict):
        raise ValueError("The routine must be an object")
    name = _name(body.get("name"), field="name", required=True)
    steps_raw = body.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("A routine needs at least one step")
    if len(steps_raw) > MAX_STEPS:
        raise ValueError(f"A routine can have at most {MAX_STEPS} steps")
    return RoutineDraft(name=name, steps=tuple(_step(step, index) for index, step in enumerate(steps_raw, start=1)))


class _TidClock:
    """Millisecond tids like the app's, strictly increasing within a save."""

    def __init__(self, now_ms: Callable[[], int]) -> None:
        self._now_ms = now_ms
        self._last = 0

    def next(self) -> str:
        self._last = max(self._now_ms(), self._last + 1)
        return str(self._last)


def _default_now_ms() -> int:
    return time.time_ns() // 1_000_000


def _existing_entries(existing_param: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Data entries of an existing scene by tid."""
    if not existing_param:
        return {}
    try:
        steps = parse_scene_steps({"id": 0, "param": json.dumps(existing_param)})
    except RoutineExecutionError:
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for step in steps:
        data = step.params.get("data") if isinstance(step.params, dict) else None
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict) and str(entry.get("tid") or "").strip():
                entries[str(entry["tid"]).strip()] = entry
    return entries


def _step_name(step: RoutineStepDraft) -> str:
    if step.name:
        return step.name
    if step.kind == "rooms":
        return "Rooms"
    return "Zone"


def build_scene_param(
    *,
    device_id: str,
    draft: RoutineDraft,
    existing_param: Mapping[str, Any] | None = None,
    now_ms: Callable[[], int] = _default_now_ms,
) -> dict[str, Any]:
    """The scene param for a draft, keeping the existing param's other fields."""
    existing_entries = _existing_entries(existing_param)
    tids = _TidClock(now_ms)
    items: list[dict[str, Any]] = []
    for number, step in enumerate(draft.steps, start=1):
        settings = {
            "fan_power": step.fan_power,
            "water_box_mode": step.water_box_mode,
            "mop_mode": step.mop_mode,
            "repeat": step.repeat,
        }
        if step.kind == "rooms":
            entry: dict[str, Any] = {
                "tid": tids.next(),
                "segs": [{"sid": room} for room in step.rooms],
                "map_flag": 0,
                **settings,
            }
        elif step.keep_tid:
            kept = existing_entries.get(step.keep_tid)
            if kept is None or not isinstance(kept.get("zones"), list) or not kept["zones"]:
                raise ValueError(f"step {number} refers to zones this routine does not have")
            entry = {
                "tid": step.keep_tid,
                "zones": [{**zone, "repeat": step.repeat} for zone in kept["zones"] if isinstance(zone, dict)],
                "map_flag": kept.get("map_flag", 0),
                **settings,
            }
        else:
            entry = {
                "tid": tids.next(),
                "zones": [
                    {"zid": zid, "repeat": step.repeat, "range": list(zone)}
                    for zid, zone in enumerate(step.zones)
                ],
                "map_flag": 0,
                **settings,
            }
        command = {
            "id": number,
            "method": _STEP_METHODS[step.kind],
            "params": {"data": [entry], "source": _SCENE_SOURCE},
        }
        items.append(
            {
                "id": number,
                "type": "CMD",
                "name": _step_name(step),
                "entityId": device_id,
                "param": json.dumps(command, separators=(",", ":")),
                "finishDpIds": [_STEP_FINISHED_DP],
            }
        )

    param: dict[str, Any] = {"triggers": [], "matchType": "NONE"}
    if existing_param:
        param.update({key: value for key, value in existing_param.items() if key != "action"})
    existing_action = existing_param.get("action") if existing_param else None
    action = dict(existing_action) if isinstance(existing_action, dict) else {"type": "S"}
    action["items"] = items
    param["action"] = action
    return param


def _scene_param(scene: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = scene.get("param")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _settings_of(entry: Mapping[str, Any]) -> dict[str, int]:
    def value(field: str, default: int) -> int:
        try:
            return int(entry.get(field, default))
        except (TypeError, ValueError):
            return default

    return {
        "fan_power": value("fan_power", DEFAULT_FAN_POWER),
        "water_box_mode": value("water_box_mode", DEFAULT_WATER_BOX_MODE),
        "mop_mode": value("mop_mode", DEFAULT_MOP_MODE),
        "repeat": max(1, min(MAX_REPEAT, value("repeat", 1))),
    }


def _editable_step(step_name: str, method: str, params: Any) -> dict[str, Any] | None:
    data = params.get("data") if isinstance(params, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return None
    entry = data[0]
    step: dict[str, Any] = {"name": step_name, **_settings_of(entry)}
    if method == "do_scenes_segments":
        segs = entry.get("segs")
        rooms = []
        for seg in segs if isinstance(segs, list) else []:
            try:
                rooms.append(int(seg.get("sid") if isinstance(seg, dict) else seg))
            except (TypeError, ValueError):
                return None
        if not rooms:
            return None
        return {"kind": "rooms", "rooms": rooms, **step}
    if method == "do_scenes_zones":
        zones = entry.get("zones")
        if not isinstance(zones, list) or not zones:
            return None
        ranges = []
        for zone in zones:
            range_value = zone.get("range") if isinstance(zone, dict) else None
            if not isinstance(range_value, list) or len(range_value) < 4:
                ranges = []
                break
            try:
                ranges.append([int(value) for value in range_value[:4]])
            except (TypeError, ValueError):
                ranges = []
                break
        tid = str(entry.get("tid") or "").strip()
        # Zones saved in the Roborock app live on the robot; the editor keeps them as they are.
        return {"kind": "zone", "zones": ranges, "tid": tid, "zone_count": len(zones), **step}
    return None


def editable_routine(scene: Mapping[str, Any]) -> dict[str, Any]:
    """A saved scene as the editor shows it; ``editable`` is False for steps it cannot edit."""
    summary: dict[str, Any] = {
        "id": scene.get("id"),
        "name": str(scene.get("name") or "").strip(),
        "device_id": str(scene.get("device_id") or scene.get("deviceId") or "").strip(),
        "editable": False,
        "steps": [],
    }
    param = _scene_param(scene)
    if param is None:
        return summary
    try:
        steps = parse_scene_steps({"id": scene.get("id"), "param": json.dumps(param)})
    except RoutineExecutionError:
        return summary
    editable_steps = [_editable_step(step.name, step.method, step.params) for step in steps]
    if any(step is None for step in editable_steps):
        summary["steps"] = [{"kind": "other", "name": step.name} for step in steps]
        return summary
    summary["steps"] = editable_steps
    summary["editable"] = True
    return summary
