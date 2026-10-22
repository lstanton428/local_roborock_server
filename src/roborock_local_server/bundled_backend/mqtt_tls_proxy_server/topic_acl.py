"""Per-connection MQTT topic permissions for the TLS proxy.

The embedded broker accepts anonymous local connections, so the proxy is the
only place that knows who a client is. Each authenticated connection gets an
ACL describing the topics it may publish to and subscribe to:

- app / Home Assistant users: rr/m/<dir>/<rriot.u>/<mqtt username>/<duid>
- vacuums: rr/d/<dir>/<did>/<mqtt username>

Both publish on "i" and subscribe on "o": the topic bridge listens on
rr/m/i/# and rr/d/i/# and mirrors each side onto the other's "o" topic.

Because the MQTT username is derived from the client's own credentials, binding
it into the topic keeps one client from reading or writing another's traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

from roborock_local_server.config import DEFAULT_TOPIC_ACL_MODE, TOPIC_ACL_MODES

__all__ = ["DEFAULT_TOPIC_ACL_MODE", "TOPIC_ACL_MODES", "TopicAcl", "extract_subscribe_filters"]

_ANY = None  # Matches one non-empty topic level.
_PUBLISH_DIRECTION = "i"
_SUBSCRIBE_DIRECTION = "o"


@dataclass(frozen=True)
class TopicAcl:
    identity: str
    publish_levels: tuple[str | None, ...]
    subscribe_levels: tuple[str | None, ...]

    @classmethod
    def for_user(cls, mqtt_username: str) -> TopicAcl:
        return cls(
            identity=f"user:{mqtt_username}",
            publish_levels=("rr", "m", _PUBLISH_DIRECTION, _ANY, mqtt_username, _ANY),
            subscribe_levels=("rr", "m", _SUBSCRIBE_DIRECTION, _ANY, mqtt_username, _ANY),
        )

    @classmethod
    def for_device(cls, mqtt_username: str, did: str = "") -> TopicAcl:
        return cls(
            identity=f"device:{did or '?'}:{mqtt_username}",
            publish_levels=("rr", "d", _PUBLISH_DIRECTION, did or _ANY, mqtt_username),
            subscribe_levels=("rr", "d", _SUBSCRIBE_DIRECTION, did or _ANY, mqtt_username),
        )

    def allows_publish(self, topic: str) -> bool:
        parts = topic.split("/")
        if len(parts) != len(self.publish_levels):
            return False
        for part, allowed in zip(parts, self.publish_levels):
            if not part or "+" in part or "#" in part:
                return False
            if allowed is not _ANY and part != allowed:
                return False
        return True

    def allows_subscribe(self, topic_filter: str) -> bool:
        parts = topic_filter.split("/")
        if len(parts) != len(self.subscribe_levels):
            return False
        for part, allowed in zip(parts, self.subscribe_levels):
            if part == "+":
                # A single-level wildcard is fine wherever the ACL already
                # accepts any value; it cannot reach another identity because
                # the username level must still match exactly.
                if allowed is _ANY:
                    continue
                return False
            if not part or "#" in part or "+" in part:
                return False
            if allowed is not _ANY and part != allowed:
                return False
        return True


def _decode_varint(data: bytes, start: int) -> tuple[int | None, int]:
    value = 0
    multiplier = 1
    for consumed, index in enumerate(range(start, min(start + 4, len(data))), start=1):
        byte_val = data[index]
        value += (byte_val & 0x7F) * multiplier
        if not byte_val & 0x80:
            return value, consumed
        multiplier *= 128
    return None, 0


def extract_subscribe_filters(packet: bytes, protocol_level: int | None) -> list[str] | None:
    """Return the topic filters in a SUBSCRIBE packet, or None if it is malformed."""
    if not packet or packet[0] >> 4 != 8:
        return None
    remaining_len, length_bytes = _decode_varint(packet, 1)
    if remaining_len is None:
        return None
    end = 1 + length_bytes + remaining_len
    if end > len(packet):
        return None
    cursor = 1 + length_bytes + 2  # packet identifier
    if protocol_level == 5:
        properties_len, properties_bytes = _decode_varint(packet, cursor)
        if properties_len is None:
            return None
        cursor += properties_bytes + properties_len
    filters: list[str] = []
    while cursor < end:
        if cursor + 2 > end:
            return None
        filter_len = int.from_bytes(packet[cursor : cursor + 2], "big")
        cursor += 2
        if cursor + filter_len + 1 > end:
            return None
        filters.append(packet[cursor : cursor + filter_len].decode("utf-8", errors="replace"))
        cursor += filter_len + 1  # subscription options byte
    return filters if filters else None
