import asyncio
import gzip
import json
import logging
from pathlib import Path
import struct

import pytest

from roborock.protocol import Utils, create_mqtt_decoder, create_mqtt_encoder
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol
from roborock_local_server.bundled_backend.shared.context import ServerContext
import roborock_local_server.bundled_backend.shared.routine_runner as routine_runner_module
from roborock_local_server.bundled_backend.shared.vacuum_map import (
    MapTransform,
    parse_vacuum_map,
    segment_iot_ids,
)

LOCAL_KEY = "0123456789abcdef"


def synthetic_map(*, top: int = 500, left: int = 500, width: int = 40, height: int = 30) -> bytes:
    """A get_map_v1 payload with an image block holding rooms 3 and 7, and a charger."""
    pixels = bytearray(width * height)

    def fill(room: int, x0: int, x1: int, y0: int, y1: int) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                pixels[y * width + x] = (room << 3) | 7

    fill(3, 5, 14, 5, 14)
    fill(7, 20, 34, 10, 24)
    image = struct.pack("<HHI", 2, 24, len(pixels)) + struct.pack("<iiii", top, left, height, width) + bytes(pixels)
    charger = struct.pack("<HHI", 1, 8, 8) + struct.pack("<ii", 26000, 26000)
    return struct.pack("<HH", 0x7272, 20) + bytes(16) + image + charger


def test_transform_follows_the_parser_calibration() -> None:
    vacuum_map = parse_vacuum_map(synthetic_map(), {}, {})
    transform = vacuum_map.transform

    # The image starts at 500 map units (25,000 mm), 50 mm per unit, drawn at scale 4, y flipped.
    assert (vacuum_map.width, vacuum_map.height) == (160, 120)
    assert transform.pixel(25500, 25500) == pytest.approx((40, 76))
    assert transform.pixel(26500, 25500) == pytest.approx((120, 76))
    assert transform.pixel(25500, 26500) == pytest.approx((40, -4))
    assert transform.mm(40, 76) == pytest.approx((25500, 25500))
    for x, y in ((31200, 23000), (25000, 26450), (27999, 25001)):
        assert transform.mm(*transform.pixel(x, y)) == pytest.approx((x, y))
    assert vacuum_map.as_dict()["to_mm"] == pytest.approx([12.5, 0, 25000, 0, -12.5, 26450])


def test_transform_handles_any_three_calibration_points() -> None:
    points = [
        {"vacuum": {"x": 0, "y": 0}, "map": {"x": 10, "y": 20}},
        {"vacuum": {"x": 1000, "y": 0}, "map": {"x": 10, "y": 60}},
        {"vacuum": {"x": 0, "y": 1000}, "map": {"x": 50, "y": 20}},
    ]
    # A rotated map: x runs down the image and y runs right.
    transform = MapTransform.from_calibration(points)

    assert transform.pixel(500, 250) == pytest.approx((20, 40))
    assert transform.mm(20, 40) == pytest.approx((500, 250))
    assert transform.pixel_box(0, 0, 1000, 1000) == [10, 20, 50, 60]
    with pytest.raises(ValueError, match="collinear"):
        MapTransform.from_calibration([points[0], points[0], points[1]])


def test_rooms_get_inventory_names_and_pixel_labels() -> None:
    vacuum_map = parse_vacuum_map(
        synthetic_map(),
        {3: "11100845", 7: "11100849", 9: "11100850"},
        {"11100845": "Living room", "11100849": "Kitchen"},
    )

    rooms = {room.segment_id: room for room in vacuum_map.rooms}
    assert rooms[3].name == "Living room" and rooms[7].name == "Kitchen"
    assert rooms[3].box == [20, 60, 56, 96] and rooms[3].label == (38, 78)
    # Mapped but not drawn, e.g. a room on another floor.
    assert rooms[9].name == "Room 9" and rooms[9].label is None
    assert vacuum_map.image_png.startswith(b"\x89PNG")


def test_segment_ids_come_from_every_room_mapping_shape() -> None:
    assert segment_iot_ids([[16, "11100845", 14], [17, "11100849", 1]]) == {16: "11100845", 17: "11100849"}
    assert segment_iot_ids([[16, 11100845]]) == {16: "11100845"}
    assert segment_iot_ids([16, "11100845"]) == {16: "11100845"}
    assert segment_iot_ids([["x", "1"], [0, "2"], "junk"]) == {}
    assert segment_iot_ids({"result": "unexpected"}) == {}


def _context(tmp_path: Path) -> ServerContext:
    return ServerContext(
        api_host="api.example.com", mqtt_host="mqtt.example.com", wood_host="wood.example.com",
        region="us", protocol_login_email="user@example.com", localkey=LOCAL_KEY, duid="duid-1",
        mqtt_usr="mqtt-user", mqtt_passwd="mqtt-pass", mqtt_clientid="mqtt-client",
        https_port=443, mqtt_tls_port=8883,
        http_jsonl=tmp_path / "http.jsonl", mqtt_jsonl=tmp_path / "mqtt.jsonl",
        loggers={"api": logging.getLogger("test-vacuum-map")},
    )


class _Vacuum:
    """Answers the client's MQTT requests the way the robot does."""

    def __init__(self, client, *, map_bytes: bytes, endpoint: str | None = None, refuse: bool = False) -> None:
        self._client = client
        self._map_bytes = map_bytes
        self._endpoint = endpoint
        self._refuse = refuse
        self._encoder = create_mqtt_encoder(LOCAL_KEY)
        self.methods: list[str] = []

    async def publish(self, topic: str, payload: bytes) -> None:
        (request,) = create_mqtt_decoder(LOCAL_KEY)(payload)
        inner = json.loads(json.loads(request.payload)["dps"]["101"])
        self.methods.append(inner["method"])
        if self._refuse:
            reply = {"dps": {"102": json.dumps({"id": inner["id"], "error": {"code": -10005, "message": "busy"}})}}
            message = RoborockMessage(protocol=RoborockMessageProtocol.RPC_RESPONSE, payload=json.dumps(reply).encode())
        else:
            endpoint = (self._endpoint or inner["security"]["endpoint"]).encode()
            header = struct.pack("<8s8sH6s", endpoint, bytes(8), inner["id"], bytes(6))
            body = Utils.encrypt_cbc(gzip.compress(self._map_bytes), bytes.fromhex(inner["security"]["nonce"]))
            message = RoborockMessage(protocol=RoborockMessageProtocol.MAP_RESPONSE, payload=header + body)
        asyncio.get_running_loop().call_soon(self._client._on_message, self._encoder(message))


def _client(tmp_path: Path, **vacuum_options):
    client = routine_runner_module._RoutineMqttClient(
        _context(tmp_path), {"duid": "duid-1", "localkey": LOCAL_KEY}, logging.getLogger("test-vacuum-map")
    )
    client._session = _Vacuum(client, **vacuum_options)
    return client


def test_client_reads_the_map_from_a_protocol_301_reply(tmp_path: Path) -> None:
    raw_map = synthetic_map()
    client = _client(tmp_path, map_bytes=raw_map)

    received = asyncio.run(client.get_map_v1(timeout=2))

    assert received == raw_map
    assert client._session.methods == ["get_map_v1"]
    assert parse_vacuum_map(received, {}, {}).width == 160


def test_client_ignores_maps_meant_for_other_clients(tmp_path: Path) -> None:
    client = _client(tmp_path, map_bytes=synthetic_map(), endpoint="AAAAAAAA")

    with pytest.raises(routine_runner_module._CommandTimeout):
        asyncio.run(client.get_map_v1(timeout=0.3))


def test_client_fails_a_map_request_the_robot_refuses(tmp_path: Path) -> None:
    client = _client(tmp_path, map_bytes=b"", refuse=True)

    with pytest.raises(Exception, match="busy"):
        asyncio.run(client.get_map_v1(timeout=2))
