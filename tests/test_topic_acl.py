import logging
from pathlib import Path

import pytest

from roborock_local_server.backend import MqttTlsProxy
from roborock_local_server.bundled_backend.mqtt_tls_proxy_server.topic_acl import (
    TopicAcl,
    extract_subscribe_filters,
)
from test_mqtt_tls_proxy import _FakeDestinationSocket, _FakeSourceSocket, _build_publish_packet


def _build_subscribe_packet(*filters: str, protocol_level: int = 4) -> bytes:
    body = (7).to_bytes(2, "big")
    if protocol_level == 5:
        body += b"\x02\x0b\x01"  # properties: subscription identifier = 1
    for topic_filter in filters:
        encoded = topic_filter.encode()
        body += len(encoded).to_bytes(2, "big") + encoded + b"\x01"
    return bytes([0x82, len(body)]) + body


def test_user_acl_limits_topics_to_own_username() -> None:
    acl = TopicAcl.for_user("c0ffee")

    assert acl.allows_publish("rr/m/i/hawk-user/c0ffee/duid-1")
    assert acl.allows_subscribe("rr/m/o/hawk-user/c0ffee/duid-1")
    assert acl.allows_subscribe("rr/m/o/+/c0ffee/+")
    assert acl.allows_subscribe("rr/m/+/hawk-user/c0ffee/duid-1")

    assert not acl.allows_publish("rr/m/i/hawk-user/someone-else/duid-1")
    assert not acl.allows_publish("rr/d/i/1103821560705/c0ffee")
    assert not acl.allows_publish("rr/m/x/hawk-user/c0ffee/duid-1")
    assert not acl.allows_publish("rr/m/i/hawk-user/c0ffee/+")
    assert not acl.allows_publish("")
    assert not acl.allows_subscribe("#")
    assert not acl.allows_subscribe("rr/#")
    assert not acl.allows_subscribe("rr/m/o/+/+/+")
    assert not acl.allows_subscribe("$SYS/broker/clients")


def test_device_acl_binds_did_and_username() -> None:
    acl = TopicAcl.for_device("c25b14ceac358d2a", "1103821560705")

    assert acl.allows_publish("rr/d/i/1103821560705/c25b14ceac358d2a")
    assert acl.allows_subscribe("rr/d/o/1103821560705/c25b14ceac358d2a")
    assert not acl.allows_publish("rr/d/i/9999999999999/c25b14ceac358d2a")
    assert not acl.allows_subscribe("rr/d/o/+/c25b14ceac358d2a")
    assert not acl.allows_publish("rr/m/i/u/c25b14ceac358d2a/duid")

    unknown_did = TopicAcl.for_device("c25b14ceac358d2a")
    assert unknown_did.allows_publish("rr/d/i/9999999999999/c25b14ceac358d2a")
    assert not unknown_did.allows_publish("rr/d/i/9999999999999/other-user")


@pytest.mark.parametrize("protocol_level", [4, 5])
def test_extract_subscribe_filters(protocol_level: int) -> None:
    packet = _build_subscribe_packet("rr/m/o/u/name/duid", "#", protocol_level=protocol_level)

    assert extract_subscribe_filters(packet, protocol_level) == ["rr/m/o/u/name/duid", "#"]
    assert extract_subscribe_filters(packet[:-3], protocol_level) is None
    assert extract_subscribe_filters(_build_publish_packet(topic="a"), protocol_level) is None


def _proxy(tmp_path: Path, mode: str) -> MqttTlsProxy:
    proxy = MqttTlsProxy(
        cert_file=tmp_path / "fullchain.pem",
        key_file=tmp_path / "privkey.pem",
        listen_host="127.0.0.1",
        listen_port=8883,
        backend_host="127.0.0.1",
        backend_port=1883,
        localkey="test-local-key",
        logger=logging.getLogger("test.topic_acl"),
        decoded_jsonl=tmp_path / "decoded.jsonl",
        topic_acl_mode=mode,
    )
    proxy._conn_topic_acls["1"] = TopicAcl.for_user("c0ffee")
    proxy._set_conn_protocol_level("1", 4)
    proxy._running = True
    return proxy


def test_enforce_mode_disconnects_before_forwarding_a_denied_subscribe(tmp_path: Path, caplog) -> None:
    proxy = _proxy(tmp_path, "enforce")
    allowed = _build_subscribe_packet("rr/m/o/hawk-user/c0ffee/duid-1")
    denied = _build_subscribe_packet("#")
    src = _FakeSourceSocket(allowed, denied, _build_publish_packet(topic="rr/m/i/hawk-user/c0ffee/duid-1"))
    dst = _FakeDestinationSocket()

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._relay(src, dst, "1", "c2b", bytearray())

    assert dst.sent == [allowed]
    assert src.closed and dst.closed
    assert "topic ACL blocked SUBSCRIBE filters=#" in caplog.text
    assert "identity=user:c0ffee" in caplog.text
    proxy.stop()


def test_enforce_mode_blocks_publish_completed_in_a_later_chunk(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path, "enforce")
    denied = _build_publish_packet(topic="rr/m/i/hawk-user/someone-else/duid-1")
    src = _FakeSourceSocket(denied[:5], denied[5:])
    dst = _FakeDestinationSocket()

    proxy._relay(src, dst, "1", "c2b", bytearray())

    # Only the incomplete first fragment reached the broker before the disconnect.
    assert dst.sent == [denied[:5]]
    proxy.stop()


def test_audit_mode_logs_but_forwards(tmp_path: Path, caplog) -> None:
    proxy = _proxy(tmp_path, "audit")
    denied = _build_publish_packet(topic="rr/m/i/hawk-user/someone-else/duid-1")
    src = _FakeSourceSocket(denied)
    dst = _FakeDestinationSocket()

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._relay(src, dst, "1", "c2b", bytearray())

    assert dst.sent == [denied]
    assert "topic ACL would block PUBLISH topic=rr/m/i/hawk-user/someone-else/duid-1" in caplog.text
    proxy.stop()


def test_acl_identity_follows_authorization_reason(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path, "audit")

    user = proxy._topic_acl_for_connection(
        auth_reason="user_hash", connect_info={"username": "c0ffee"}, onboarding_candidate=None
    )
    assert user is not None and user.identity == "user:c0ffee"
    assert user.allows_publish("rr/m/i/hawk-user/c0ffee/duid-1")
    assert proxy._topic_acl_for_connection(
        auth_reason="bootstrap", connect_info={"username": "boot"}, onboarding_candidate=None
    ) is None
    onboarding = proxy._topic_acl_for_connection(
        auth_reason="device_mqtt_onboarding_pending",
        connect_info={"username": "c25b14ceac358d2a"},
        onboarding_candidate={"did": "1103821560705"},
    )
    assert onboarding is not None and onboarding.identity == "device:1103821560705:c25b14ceac358d2a"
    assert not onboarding.allows_publish("rr/d/i/9999999999999/c25b14ceac358d2a")
    proxy.stop()


def test_proxy_rejects_unknown_acl_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="topic_acl_mode"):
        _proxy(tmp_path, "strict")
