import json
from pathlib import Path

import pytest

from roborock.roborock_typing import RoborockCommand
from roborock_local_server.bundled_backend.shared.routine_editor import (
    build_scene_param,
    editable_routine,
    parse_routine_draft,
)
from roborock_local_server.bundled_backend.shared.routine_runner import commands_for_step, parse_scene_steps

DUID = "1edNG6z7iLi8GWUJUDL0i8"
APP_FIXTURE = Path(__file__).parent / "contracts" / "fixtures" / "ios_app_init_v4_59_02_anonymized.json"


def _clock(start: int = 1761400000000):
    ticks = iter(range(start, start + 100))
    return lambda: next(ticks)


def _items(param: dict) -> list[tuple[dict, dict]]:
    return [(item, json.loads(item["param"])) for item in param["action"]["items"]]


def _draft(*steps: dict, name: str = "Evening") -> object:
    return parse_routine_draft({"name": name, "steps": list(steps)})


def test_scene_param_matches_the_app_format() -> None:
    draft = _draft(
        {"kind": "rooms", "rooms": [3, 7], "fan_power": 104, "water_box_mode": 200, "name": "Living room, Kitchen"},
        {"kind": "zone", "zones": [[33000, 25600, 31200, 23000]], "fan_power": 108, "repeat": 2},
    )

    param = build_scene_param(device_id=DUID, draft=draft, now_ms=_clock())

    assert param["triggers"] == [] and param["matchType"] == "NONE" and param["action"]["type"] == "S"
    (rooms_item, rooms_cmd), (zone_item, zone_cmd) = _items(param)
    for number, item in enumerate((rooms_item, zone_item), start=1):
        assert {key: item[key] for key in ("id", "type", "entityId", "finishDpIds")} == {
            "id": number, "type": "CMD", "entityId": DUID, "finishDpIds": [130],
        }
    assert rooms_item["name"] == "Living room, Kitchen" and zone_item["name"] == "Zone"
    assert rooms_cmd == {
        "id": 1,
        "method": "do_scenes_segments",
        "params": {
            "data": [{
                "tid": "1761400000000", "segs": [{"sid": 3}, {"sid": 7}], "map_flag": 0,
                "fan_power": 104, "water_box_mode": 200, "mop_mode": 300, "repeat": 1,
            }],
            "source": 101,
        },
    }
    # Corners are normalized to x1 < x2, y1 < y2 and carry the range in mm.
    assert zone_cmd["method"] == "do_scenes_zones"
    assert zone_cmd["params"]["data"] == [{
        "tid": "1761400000001",
        "zones": [{"zid": 0, "repeat": 2, "range": [31200, 23000, 33000, 25600]}],
        "map_flag": 0, "fan_power": 108, "water_box_mode": 200, "mop_mode": 300, "repeat": 2,
    }]


def test_each_step_gets_its_own_millisecond_tid() -> None:
    draft = _draft(*[{"kind": "rooms", "rooms": [room]} for room in (1, 2, 3)])

    param = build_scene_param(device_id=DUID, draft=draft, now_ms=lambda: 1761400000000)

    tids = [cmd["params"]["data"][0]["tid"] for _, cmd in _items(param)]
    assert tids == ["1761400000000", "1761400000001", "1761400000002"]


def test_the_runner_runs_saved_steps_vacuum_only() -> None:
    draft = _draft(
        {"kind": "rooms", "rooms": [3, 6], "fan_power": 104},
        {"kind": "zone", "zones": [[31200, 23000, 33000, 25600], [20000, 20000, 21000, 21000]]},
    )
    scene = {"id": 9, "name": "Evening", "device_id": DUID,
             "param": json.dumps(build_scene_param(device_id=DUID, draft=draft, now_ms=_clock()))}

    rooms_step, zone_step = parse_scene_steps(scene)

    assert [(c.command, c.params) for c in commands_for_step(rooms_step)] == [
        (RoborockCommand.SET_CUSTOM_MODE, [104]),
        (RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [200]),
        (RoborockCommand.SET_MOP_MODE, [300]),
        (RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": [3, 6], "repeat": 1}]),
    ]
    # The robot never stored these zones, so the runner cleans the coordinates.
    assert [(c.command, c.params) for c in commands_for_step(zone_step, stored_zones={})][-1] == (
        RoborockCommand.APP_ZONED_CLEAN,
        [[31200, 23000, 33000, 25600, 1], [20000, 20000, 21000, 21000, 1]],
    )
    assert (RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [200]) in [
        (c.command, c.params) for c in commands_for_step(zone_step, stored_zones={})
    ]


def _app_scenes() -> list[dict]:
    fixture = json.loads(APP_FIXTURE.read_text())
    scenes: dict[str, dict] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("param"), str) and "do_scenes_" in value["param"] and "name" in value:
                scenes[value["name"]] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(fixture)
    return list(scenes.values())


def test_app_routines_load_into_the_editor() -> None:
    scenes = {scene["name"]: editable_routine(scene) for scene in _app_scenes()}

    rooms = scenes["After Dinner"]
    assert rooms["editable"] is True
    assert rooms["steps"] == [{
        "kind": "rooms", "rooms": [16], "name": "Living Room",
        "fan_power": 101, "water_box_mode": 200, "mop_mode": 300, "repeat": 2,
    }]
    zone = scenes["Morning Refresh"]["steps"][0]
    # The app keeps the zone's coordinates on the robot, so only its id is known.
    assert zone["kind"] == "zone" and zone["zones"] == [] and zone["zone_count"] == 1
    assert zone["tid"] == "1712345602000"


def test_editing_keeps_app_zones_and_the_rest_of_the_param() -> None:
    scene = next(scene for scene in _app_scenes() if scene["name"] == "Morning Refresh")
    existing = json.loads(scene["param"])
    existing["tagId"] = "42"
    draft = _draft(
        {"kind": "zone", "keep_tid": "1712345602000", "fan_power": 104, "water_box_mode": 200, "repeat": 2},
        {"kind": "rooms", "rooms": [16]},
    )

    param = build_scene_param(device_id="anon-device-qrevo", draft=draft, existing_param=existing, now_ms=_clock())

    assert param["tagId"] == "42"
    (_, zone_cmd), (_, rooms_cmd) = _items(param)
    entry = zone_cmd["params"]["data"][0]
    assert entry["tid"] == "1712345602000"
    assert entry["zones"] == [{"zid": 8, "repeat": 2}]
    assert (entry["fan_power"], entry["water_box_mode"], entry["repeat"]) == (104, 200, 2)
    assert rooms_cmd["params"]["data"][0]["tid"] == "1761400000000"
    # The runner still sends this zone the native way when the robot stores it.
    step = parse_scene_steps({"id": 1, "param": json.dumps(param)})[0]
    (native,) = commands_for_step(step, stored_zones={"1712345602000": {8}})
    assert native.command == "do_scenes_zones" and native.params["source"] == 101

    with pytest.raises(ValueError, match="zones this routine does not have"):
        build_scene_param(
            device_id="anon-device-qrevo",
            draft=_draft({"kind": "zone", "keep_tid": "999"}),
            existing_param=existing,
        )


def test_saved_routine_round_trips_through_the_editor() -> None:
    draft = _draft(
        {"kind": "rooms", "rooms": [7, 1], "fan_power": 103, "mop_mode": 301, "name": "Kitchen, Bedroom"},
        {"kind": "zone", "zones": [[20000, 20000, 21000, 21500]], "water_box_mode": 200},
    )
    scene = {"id": 3, "name": "Evening", "device_id": DUID,
             "param": json.dumps(build_scene_param(device_id=DUID, draft=draft, now_ms=_clock()))}

    loaded = editable_routine(scene)

    assert loaded["editable"] is True and loaded["device_id"] == DUID
    assert loaded["steps"][0] == {
        "kind": "rooms", "rooms": [7, 1], "name": "Kitchen, Bedroom",
        "fan_power": 103, "water_box_mode": 200, "mop_mode": 301, "repeat": 1,
    }
    assert loaded["steps"][1]["zones"] == [[20000, 20000, 21000, 21500]]


def test_whole_home_routines_are_listed_but_not_editable() -> None:
    scene = {
        "id": 1, "name": "Full clean", "device_id": DUID,
        "param": '{"action":{"items":[{"id":1,"type":"CMD","name":"Start","finishDpIds":[130],'
                 '"param":{"method":"do_scenes_app_start","params":[{"repeat":1}]}}]}}',
    }

    loaded = editable_routine(scene)

    assert loaded["editable"] is False
    assert loaded["steps"] == [{"kind": "other", "name": "Start"}]
    assert editable_routine({"id": 2, "name": "Broken", "param": "{"})["editable"] is False


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"steps": [{"kind": "rooms", "rooms": [1]}]}, "name is required"),
        ({"name": "x" * 65, "steps": [{"kind": "rooms", "rooms": [1]}]}, "longer than 64"),
        ({"name": "A", "steps": []}, "at least one step"),
        ({"name": "A", "steps": [{"kind": "mop"}]}, "rooms or zone"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": []}]}, "at least one room"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": [1, 1]}]}, "distinct"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": ["one"]}]}, "must be a number"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": [1], "fan_power": 999}]}, "fan_power 999"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": [1], "water_box_mode": True}]}, "water_box_mode"),
        ({"name": "A", "steps": [{"kind": "rooms", "rooms": [1], "repeat": 4}]}, "repeat 4"),
        ({"name": "A", "steps": [{"kind": "zone", "zones": []}]}, "at least one zone"),
        ({"name": "A", "steps": [{"kind": "zone", "zones": [[1, 2, 3]]}]}, r"\[x1, y1, x2, y2\]"),
        ({"name": "A", "steps": [{"kind": "zone", "zones": [[0, 0, 100, 100]]}]}, "smaller than 200 mm"),
        ({"name": "A", "steps": [{"kind": "zone", "zones": [[-500, 0, 1000, 1000]]}]}, "outside the map"),
        ({"name": "A", "steps": [{"kind": "zone", "zones": [[0, 0, 1000, 1000]] * 6}]}, "more than 5 zones"),
        ({"name": "A", "steps": [{"kind": "zone", "keep_tid": "abc"}]}, "keep_tid"),
    ],
)
def test_invalid_routines_are_refused(body: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_routine_draft(body)
