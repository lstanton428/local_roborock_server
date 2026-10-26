import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roborock_local_server import routine_panel as routine_panel_module
from roborock_local_server.backend import ServerContext, default_endpoint_rules, resolve_route
from roborock_local_server.ingress import HA_INGRESS_PROXY, build_ingress_app
from roborock_local_server.routine_panel import RoutinePanel
from roborock_local_server.bundled_backend.shared.vacuum_map import parse_vacuum_map
from https_server.routes.user.scene import service as scene_service

from test_vacuum_map import synthetic_map

DUID = "1edNG6z7iLi8GWUJUDL0i8"
ADMIN_URL = "https://api-roborock.example.com:555/admin"
APP_ZONE_PARAM = json.dumps({
    "triggers": [],
    "action": {"type": "S", "items": [{
        "id": 1, "type": "CMD", "name": "Litter box", "entityId": DUID, "finishDpIds": [130],
        "param": json.dumps({"id": 1, "method": "do_scenes_zones", "params": {"data": [{
            "tid": "1744231163916", "zones": [{"zid": 3, "repeat": 1}], "map_flag": 0,
            "fan_power": 108, "water_box_mode": 200, "mop_mode": 300, "repeat": 1,
        }], "source": 101}}),
    }]},
    "matchType": "NONE",
})
FULL_CLEAN_PARAM = (
    '{"action":{"items":[{"id":1,"type":"CMD","name":"Start","finishDpIds":[130],'
    '"param":{"method":"do_scenes_app_start","params":[{"repeat":1}]}}]}}'
)


def _context(tmp_path: Path) -> ServerContext:
    return ServerContext(
        api_host="api.example.com", mqtt_host="mqtt.example.com", wood_host="wood.example.com",
        region="us", protocol_login_email="user@example.com", localkey="0123456789abcdef", duid=DUID,
        mqtt_usr="u", mqtt_passwd="p", mqtt_clientid="c", https_port=443, mqtt_tls_port=8883,
        http_jsonl=tmp_path / "http.jsonl", mqtt_jsonl=tmp_path / "mqtt.jsonl",
        loggers={"api": logging.getLogger("test-ingress-routines")},
    )


def _inventory_path(tmp_path: Path) -> Path:
    return tmp_path / "web_api_inventory.json"


def _inventory(tmp_path: Path) -> dict:
    return json.loads(_inventory_path(tmp_path).read_text())


def _panel(tmp_path: Path, *, editor: bool = True) -> TestClient:
    _inventory_path(tmp_path).write_text(json.dumps({
        "home": {"id": 77, "rooms": [{"id": 11100845, "name": "Living room"}, {"id": 11100849, "name": "Kitchen"}]},
        "devices": [{"duid": DUID, "name": "Saros 10"}],
        "scenes": [
            {"id": 4, "name": "Vacuum LR Litter", "device_id": DUID, "param": APP_ZONE_PARAM},
            {"id": 5, "name": "Full clean", "device_id": DUID, "param": FULL_CLEAN_PARAM},
        ],
        "scene_order": [5, 4],
    }))
    app = build_ingress_app(
        admin_url=ADMIN_URL,
        status=lambda: {"overall_ok": True},
        set_new_connections_enabled=None,
        routines=RoutinePanel(_context(tmp_path)),
        routine_editor=editor,
    )
    return TestClient(app, client=(HA_INGRESS_PROXY, 50000))


NEW_ROUTINE = {
    "name": "Evening",
    "device_id": DUID,
    "steps": [
        {"kind": "rooms", "rooms": [3, 7], "fan_power": 104, "water_box_mode": 200, "mop_mode": 300,
         "repeat": 1, "name": "Living room, Kitchen"},
        {"kind": "zone", "zones": [[31200, 23000, 33000, 25600]], "fan_power": 108, "water_box_mode": 200,
         "mop_mode": 300, "repeat": 2, "name": "Zone"},
    ],
}
LEVI = {"X-Remote-User-Display-Name": "Levi"}


def test_panel_lists_routines_in_app_order(tmp_path: Path) -> None:
    response = _panel(tmp_path).get("/api/routines")

    assert response.status_code == 200
    body = response.json()
    assert body["editor"] is True
    assert body["vacuums"] == [{"duid": DUID, "name": "Saros 10"}]
    assert [(routine["id"], routine["name"], routine["editable"]) for routine in body["routines"]] == [
        (5, "Full clean", False),
        (4, "Vacuum LR Litter", True),
    ]
    assert body["routines"][1]["device_name"] == "Saros 10"
    assert body["options"]["water_box_mode"]["200"] == "Off"


def test_panel_creates_routines_the_app_and_home_assistant_see(tmp_path: Path, caplog) -> None:
    client = _panel(tmp_path)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.ingress"):
        response = client.post("/api/routines", json=NEW_ROUTINE, headers=LEVI)

    assert response.status_code == 200
    created = response.json()
    assert created["id"] == 6 and created["name"] == "Evening" and created["editable"] is True
    assert "Routine 'Evening' (6) created from Home Assistant by Levi" in caplog.messages

    inventory = _inventory(tmp_path)
    scene = inventory["scenes"][-1]
    assert (scene["id"], scene["name"], scene["device_id"], scene["device_name"]) == (6, "Evening", DUID, "Saros 10")
    assert inventory["scene_order"] == [5, 4, 6]
    param = json.loads(scene["param"])
    items = param["action"]["items"]
    assert [(item["type"], item["entityId"], item["finishDpIds"]) for item in items] == [("CMD", DUID, [130])] * 2
    rooms_cmd, zone_cmd = (json.loads(item["param"]) for item in items)
    assert rooms_cmd["method"] == "do_scenes_segments" and rooms_cmd["params"]["source"] == 101
    assert rooms_cmd["params"]["data"][0]["segs"] == [{"sid": 3}, {"sid": 7}]
    zone_entry = zone_cmd["params"]["data"][0]
    assert zone_cmd["method"] == "do_scenes_zones"
    assert zone_entry["zones"] == [{"zid": 0, "repeat": 2, "range": [31200, 23000, 33000, 25600]}]
    assert zone_entry["water_box_mode"] == 200
    assert int(zone_entry["tid"]) > int(rooms_cmd["params"]["data"][0]["tid"]) > 1_700_000_000_000

    # Home Assistant's Roborock integration and the app list routines here.
    route, payload = resolve_route(
        rules=default_endpoint_rules(), context=_context(tmp_path),
        clean_path=f"/user/scene/device/{DUID}", query_params={}, body_params={}, method="GET",
    )
    assert route == "get_scenes"
    listed = {scene["id"]: scene for scene in payload["result"]}
    assert listed[6]["name"] == "Evening" and listed[6]["param"] == scene["param"]


def test_panel_edits_renames_and_deletes_routines(tmp_path: Path, caplog) -> None:
    client = _panel(tmp_path)
    edit = {
        "name": "Litter box",
        "steps": [
            {"kind": "zone", "keep_tid": "1744231163916", "fan_power": 104, "water_box_mode": 200, "repeat": 2},
            {"kind": "rooms", "rooms": [7], "fan_power": 102, "water_box_mode": 200},
        ],
    }

    with caplog.at_level(logging.INFO, logger="roborock_local_server.ingress"):
        edited = client.post("/api/routines/4", json=edit, headers=LEVI)
        renamed = client.post("/api/routines/4/rename", json={"name": "  Litter   corner "}, headers=LEVI)

    assert edited.status_code == renamed.status_code == 200
    assert renamed.json()["name"] == "Litter corner"
    scene = next(scene for scene in _inventory(tmp_path)["scenes"] if scene["id"] == 4)
    assert scene["name"] == "Litter corner"
    zone_cmd, rooms_cmd = (json.loads(item["param"]) for item in json.loads(scene["param"])["action"]["items"])
    # The zone the app saved on the robot keeps its tid, so the robot still finds it.
    assert zone_cmd["params"]["data"][0]["tid"] == "1744231163916"
    assert zone_cmd["params"]["data"][0]["zones"] == [{"zid": 3, "repeat": 2}]
    assert rooms_cmd["params"]["data"][0]["segs"] == [{"sid": 7}]
    assert "Routine 'Litter box' (4) edited from Home Assistant by Levi" in caplog.messages
    assert "Routine 'Litter box' renamed to 'Litter corner' from Home Assistant by Levi" in caplog.messages

    # Whole-home routines from the app can't be edited here, only run, renamed or deleted.
    assert client.post("/api/routines/5", json=edit).status_code == 400

    with caplog.at_level(logging.INFO, logger="roborock_local_server.ingress"):
        deleted = client.post("/api/routines/4/delete", json={}, headers=LEVI)
    assert deleted.status_code == 200
    inventory = _inventory(tmp_path)
    assert [scene["id"] for scene in inventory["scenes"]] == [5]
    assert inventory["scene_order"] == [5]
    assert "Routine 'Litter corner' deleted from Home Assistant by Levi" in caplog.messages


def test_panel_runs_routines(tmp_path: Path, monkeypatch, caplog) -> None:
    started: list[int] = []

    def fake_execute(ctx, scene_id: int) -> dict:
        started.append(scene_id)
        return {"accepted": True, "status": "started", "sceneId": scene_id}

    monkeypatch.setattr(scene_service, "execute_scene", fake_execute)
    client = _panel(tmp_path)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.ingress"):
        response = client.post("/api/routines/4/run", json={}, headers=LEVI)

    assert response.status_code == 200 and response.json()["status"] == "started"
    assert started == [4]
    assert "Routine 'Vacuum LR Litter' started from Home Assistant by Levi" in caplog.messages
    assert client.post("/api/routines/99/run", json={}).status_code == 404
    # Running is a state change too, so it needs a JSON body like every other post.
    assert client.post("/api/routines/4/run").status_code == 415
    assert started == [4]


def test_editor_turned_off_only_lists_and_runs(tmp_path: Path, monkeypatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(scene_service, "execute_scene", lambda ctx, scene_id: started.append(scene_id) or {"status": "started"})
    monkeypatch.setattr(routine_panel_module, "read_vacuum_map", pytest.fail)
    client = _panel(tmp_path, editor=False)
    before = _inventory_path(tmp_path).read_text()

    listed = client.get("/api/routines").json()
    assert listed["editor"] is False and len(listed["routines"]) == 2
    assert client.post("/api/routines/4/run", json={}).status_code == 200
    for path, body in (
        ("/api/routines", NEW_ROUTINE),
        ("/api/routines/4", NEW_ROUTINE),
        ("/api/routines/4/rename", {"name": "Renamed"}),
        ("/api/routines/4/delete", {}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 403
        assert "ingress_routine_editor" in response.json()["error"]
    assert client.get(f"/api/map?device={DUID}").status_code == 403
    assert _inventory_path(tmp_path).read_text() == before
    assert started == [4]


def test_routine_changes_are_validated(tmp_path: Path) -> None:
    client = _panel(tmp_path)
    before = _inventory_path(tmp_path).read_text()

    # A form post is what a cross-site page could send without a CORS preflight.
    assert client.post("/api/routines", data={"name": "x"}).status_code == 415
    assert client.post("/api/routines", content="{", headers={"Content-Type": "application/json"}).status_code == 400
    oversized = json.dumps({**NEW_ROUTINE, "padding": "x" * 70_000})
    assert client.post("/api/routines", content=oversized, headers={"Content-Type": "application/json"}).status_code == 413
    bad_zone = {**NEW_ROUTINE, "steps": [{"kind": "zone", "zones": [[0, 0, 50, 50]]}]}
    response = client.post("/api/routines", json=bad_zone)
    assert response.status_code == 400 and "smaller than 200 mm" in response.json()["error"]
    assert client.post("/api/routines", json={**NEW_ROUTINE, "device_id": "someone-else"}).status_code == 404
    assert client.post("/api/routines/99", json=NEW_ROUTINE).status_code == 404
    assert client.post("/api/routines/4/rename", json={"name": " "}).status_code == 400
    assert client.post("/api/routines/99/delete", json={}).status_code == 404
    assert _inventory_path(tmp_path).read_text() == before


def test_routine_endpoints_answer_only_the_ingress_proxy(tmp_path: Path) -> None:
    client = TestClient(_panel(tmp_path).app, client=("192.168.1.20", 50000))
    for response in (
        client.get("/api/routines"),
        client.get(f"/api/map?device={DUID}"),
        client.post("/api/routines", json=NEW_ROUTINE),
        client.post("/api/routines/4/run", json={}),
        client.post("/api/routines/4/delete", json={}),
    ):
        assert response.status_code == 403
    assert len(_inventory(tmp_path)["scenes"]) == 2


def test_panel_map_names_rooms_from_the_inventory(tmp_path: Path, monkeypatch) -> None:
    requested: list[dict] = []

    async def fake_read(context, device, *, room_names, logger):
        requested.append(device)
        return parse_vacuum_map(synthetic_map(), {3: "11100845", 7: "11100849"}, room_names)

    monkeypatch.setattr(routine_panel_module, "read_vacuum_map", fake_read)
    client = _panel(tmp_path)

    response = client.get(f"/api/map?device={DUID}")

    assert response.status_code == 200
    body = response.json()
    assert body["image"].startswith("data:image/png;base64,")
    assert (body["width"], body["height"]) == (160, 120)
    assert [(room["segment_id"], room["name"]) for room in body["rooms"]] == [(3, "Living room"), (7, "Kitchen")]
    assert body["to_mm"] == pytest.approx([12.5, 0, 25000, 0, -12.5, 26450])
    assert requested[0]["duid"] == DUID
    assert "localkey" not in json.dumps(body) and "0123456789abcdef" not in json.dumps(body)
    assert "img-src data:" in response.headers["content-security-policy"]
    assert client.get("/api/map?device=unknown").status_code == 404


def test_panel_reports_a_map_the_vacuum_does_not_send(tmp_path: Path, monkeypatch) -> None:
    async def offline(context, device, *, room_names, logger):
        raise routine_panel_module.RoutineExecutionError("Command get_room_mapping timed out after 15.0s")

    monkeypatch.setattr(routine_panel_module, "read_vacuum_map", offline)

    response = _panel(tmp_path).get(f"/api/map?device={DUID}")

    assert response.status_code == 502
    assert "timed out" in response.json()["error"]


def test_running_a_panel_zone_does_not_warn_about_missing_ranges(tmp_path: Path, caplog) -> None:
    client = _panel(tmp_path)
    created = client.post("/api/routines", json=NEW_ROUTINE).json()
    scenes = {scene["id"]: scene for scene in _inventory(tmp_path)["scenes"]}

    with caplog.at_level(logging.WARNING):
        scene_service._hydrate_inventory_scene_ranges(_context(tmp_path), scenes[created["id"]])
    assert not [message for message in caplog.messages if "no range data" in message]

    # App zones that only the robot knows still get the warning.
    with caplog.at_level(logging.WARNING):
        scene_service._hydrate_inventory_scene_ranges(_context(tmp_path), scenes[4])
    assert [message for message in caplog.messages if "no range data" in message]
