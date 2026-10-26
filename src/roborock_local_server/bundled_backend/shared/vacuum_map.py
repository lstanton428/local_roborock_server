"""Read a vacuum's map and room mapping over the local MQTT broker.

The routine editor draws zones on this map. Both requests only read: the robot
answers get_room_mapping over RPC and get_map_v1 with a protocol 301 message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from roborock.map.map_parser import MapParser, MapParserConfig
from roborock.roborock_typing import RoborockCommand
from vacuum_map_parser_base.config.drawable import Drawable

from .context import ServerContext
from .routine_runner import RoutineExecutionError, _RoutineMqttClient

__all__ = [
    "MapRoom",
    "MapTransform",
    "VacuumMap",
    "parse_vacuum_map",
    "read_vacuum_map",
    "segment_iot_ids",
]

_MAP_TIMEOUT_SECONDS = 20.0
_MAP_DRAWABLES = [Drawable.CHARGER, Drawable.VACUUM_POSITION]


@dataclass(frozen=True)
class MapTransform:
    """Affine map between robot coordinates (mm) and rendered map pixels.

    Built from the map parser's calibration points, so it follows the parser's
    scale, offset and y flip. ``to_px`` is (a, b, c, d, e, f) with
    ``px = a*x + b*y + c`` and ``py = d*x + e*y + f``.
    """

    to_px: tuple[float, float, float, float, float, float]

    @classmethod
    def from_calibration(cls, points: list[dict[str, dict[str, float]]]) -> MapTransform:
        if len(points) < 3:
            raise ValueError("Map calibration needs three points")
        vacuum = [(float(point["vacuum"]["x"]), float(point["vacuum"]["y"])) for point in points[:3]]
        pixels = [(float(point["map"]["x"]), float(point["map"]["y"])) for point in points[:3]]
        (x0, y0), (x1, y1), (x2, y2) = vacuum
        det = x0 * (y1 - y2) - y0 * (x1 - x2) + (x1 * y2 - x2 * y1)
        if abs(det) < 1e-9:
            raise ValueError("Map calibration points are collinear")

        def solve(targets: list[float]) -> tuple[float, float, float]:
            t0, t1, t2 = targets
            a = (t0 * (y1 - y2) - y0 * (t1 - t2) + (t1 * y2 - t2 * y1)) / det
            b = (x0 * (t1 - t2) - t0 * (x1 - x2) + (x1 * t2 - x2 * t1)) / det
            c = (x0 * (y1 * t2 - y2 * t1) - y0 * (x1 * t2 - x2 * t1) + t0 * (x1 * y2 - x2 * y1)) / det
            return a, b, c

        a, b, c = solve([pixel[0] for pixel in pixels])
        d, e, f = solve([pixel[1] for pixel in pixels])
        if abs(a * e - b * d) < 1e-12:
            raise ValueError("Map calibration cannot be inverted")
        return cls((a, b, c, d, e, f))

    @property
    def to_mm(self) -> tuple[float, float, float, float, float, float]:
        a, b, c, d, e, f = self.to_px
        det = a * e - b * d
        ia, ib, id_, ie = e / det, -b / det, -d / det, a / det
        return ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f)

    def pixel(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.to_px
        return a * x_mm + b * y_mm + c, d * x_mm + e * y_mm + f

    def mm(self, px: float, py: float) -> tuple[float, float]:
        a, b, c, d, e, f = self.to_mm
        return a * px + b * py + c, d * px + e * py + f

    def pixel_box(self, x0: float, y0: float, x1: float, y1: float) -> list[float]:
        """A mm rectangle as [left, top, right, bottom] in pixels."""
        corners = [self.pixel(x0, y0), self.pixel(x1, y1)]
        xs = [corner[0] for corner in corners]
        ys = [corner[1] for corner in corners]
        return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


@dataclass(frozen=True)
class MapRoom:
    segment_id: int
    name: str
    # Label position and bounding box in pixels; None when the room is not drawn.
    label: tuple[float, float] | None = None
    box: list[float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "name": self.name,
            "label": list(self.label) if self.label is not None else None,
            "box": self.box,
        }


@dataclass(frozen=True)
class VacuumMap:
    image_png: bytes
    width: int
    height: int
    transform: MapTransform
    rooms: list[MapRoom] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Everything but the image, for the panel."""
        return {
            "width": self.width,
            "height": self.height,
            "to_px": list(self.transform.to_px),
            "to_mm": list(self.transform.to_mm),
            "rooms": [room.as_dict() for room in self.rooms],
        }


def segment_iot_ids(response: Any) -> dict[int, str]:
    """Segment id to cloud room id, from get_room_mapping.

    Firmware answers ``[[segment_id, "iot_id", room_type], ...]``, older firmware
    leaves out the type, and a single room may come back as a flat pair.
    """
    if not isinstance(response, list):
        return {}
    if len(response) >= 2 and not isinstance(response[0], list):
        response = [response]
    segments: dict[int, str] = {}
    for entry in response:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        try:
            segment_id = int(entry[0])
        except (TypeError, ValueError):
            continue
        if segment_id > 0:
            segments[segment_id] = str(entry[1])
    return segments


def parse_vacuum_map(
    raw_map: bytes,
    segments: Mapping[int, str],
    room_names: Mapping[str, str],
) -> VacuumMap:
    """Render get_map_v1 data and name its rooms.

    ``segments`` maps segment id to cloud room id (get_room_mapping) and
    ``room_names`` maps cloud room id to the name in the inventory.
    """
    parsed = MapParser(MapParserConfig(drawables=list(_MAP_DRAWABLES))).parse(raw_map)
    map_data = parsed.map_data if parsed is not None else None
    if parsed is None or parsed.image_content is None or map_data is None or map_data.image is None:
        raise RoutineExecutionError("The vacuum sent a map without an image")
    calibration = map_data.calibration()
    if not calibration:
        raise RoutineExecutionError("The vacuum's map has no calibration")
    transform = MapTransform.from_calibration(calibration)
    width, height = map_data.image.data.size

    drawn = map_data.rooms or {}
    rooms: list[MapRoom] = []
    for segment_id in sorted(set(segments) | set(drawn)):
        iot_id = segments.get(segment_id, "")
        name = str(room_names.get(iot_id) or "").strip() or f"Room {segment_id}"
        room = drawn.get(segment_id)
        if room is None:
            rooms.append(MapRoom(segment_id=segment_id, name=name))
            continue
        box = transform.pixel_box(room.x0, room.y0, room.x1, room.y1)
        label = (round((box[0] + box[2]) / 2, 1), round((box[1] + box[3]) / 2, 1))
        rooms.append(MapRoom(segment_id=segment_id, name=name, label=label, box=box))
    return VacuumMap(
        image_png=parsed.image_content,
        width=int(width),
        height=int(height),
        transform=transform,
        rooms=rooms,
    )


async def read_vacuum_map(
    context: ServerContext,
    device: dict[str, str],
    *,
    room_names: Mapping[str, str],
    logger: logging.Logger | logging.LoggerAdapter,
) -> VacuumMap:
    """Ask the vacuum for its room mapping and current map; sends nothing that moves it."""
    client = _RoutineMqttClient(context, device, logger)
    await client.connect()
    try:
        mapping = await client.send_command(RoborockCommand.GET_ROOM_MAPPING)
        raw_map = await client.get_map_v1(timeout=_MAP_TIMEOUT_SECONDS)
    finally:
        await client.close()
    return parse_vacuum_map(raw_map, segment_iot_ids(mapping), room_names)
