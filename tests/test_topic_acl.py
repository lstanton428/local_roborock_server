import logging
from pathlib import Path

import pytest

from roborock_local_server.backend import MqttTlsProxy
from roborock_local_server.bundled_backend.mqtt_tls_proxy_server.topic_acl import (
    TopicAcl,
    extract_subscribe_filters,
)
from roborock_local_server.bundled_backend.shared.runtime_credentials import parse_mqtt_connect_packet
from test_mqtt_tls_proxy import (
    _build_connect_packet,
    _build_publish_packet,
    _FakeBackendSocket,
    _FakeDestinationSocket,
    _FakeSourceSocket,
)


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

    # Users send commands on "i" and read replies on "o", never the reverse.
    assert not acl.allows_publish("rr/m/o/hawk-user/c0ffee/duid-1")
    assert not acl.allows_subscribe("rr/m/i/hawk-user/c0ffee/duid-1")
    assert not acl.allows_subscribe("rr/m/+/hawk-user/c0ffee/duid-1")
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
    # Vacuums publish uplink on "i" (what the bridge and onboarding listen on)
    # and receive commands mirrored onto "o".
    assert not acl.allows_publish("rr/d/o/1103821560705/c25b14ceac358d2a")
    assert not acl.allows_subscribe("rr/d/i/1103821560705/c25b14ceac358d2a")
    assert not acl.allows_subscribe("rr/d/+/1103821560705/c25b14ceac358d2a")
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


def test_proxy_defaults_to_the_config_default_mode(tmp_path: Path) -> None:
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
    )

    assert proxy.topic_acl_mode == "audit"


def _build_will_connect_packet(*, will_topic: str, protocol_level: int = 4) -> bytes:
    def field(value: bytes) -> bytes:
        return len(value).to_bytes(2, "big") + value

    # clean session + will + username + password
    variable_header = field(b"MQTT") + bytes([protocol_level, 0xC6]) + (60).to_bytes(2, "big")
    if protocol_level == 5:
        variable_header += b"\x00"
    payload = field(b"ha-client")
    if protocol_level == 5:
        payload += b"\x00"  # will properties
    payload += field(will_topic.encode()) + field(b"offline") + field(b"c0ffee") + field(b"user-pass")
    remaining = variable_header + payload
    return bytes([0x10, len(remaining)]) + remaining


@pytest.mark.parametrize("protocol_level", [4, 5])
def test_parse_connect_returns_will_topic(protocol_level: int) -> None:
    parsed = parse_mqtt_connect_packet(_build_will_connect_packet(will_topic="rr/m/o/u/x/y", protocol_level=protocol_level))

    assert parsed is not None
    assert parsed["will_topic"] == "rr/m/o/u/x/y"
    assert parsed["username"] == "c0ffee"
    assert parsed["password"] == "user-pass"
    no_will = parse_mqtt_connect_packet(_build_connect_packet(client_id="a", username="b", password="c"))
    assert no_will is not None and no_will["will_topic"] is None


def _client_proxy(tmp_path: Path, mode: str, monkeypatch) -> tuple[MqttTlsProxy, _FakeBackendSocket]:
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
    backend = _FakeBackendSocket()
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: backend)
    monkeypatch.setattr(proxy, "_queue_trace_packet", lambda *args: None)
    # Sign every client in as user "c0ffee" so the test exercises only the ACL.
    monkeypatch.setattr(
        proxy,
        "_authorize_connect_packet_for_client",
        lambda packet, *, client_ip: (True, "user_hash", parse_mqtt_connect_packet(packet), None),
    )
    proxy._running = True
    return proxy, backend


def test_enforce_mode_refuses_connect_with_a_will_outside_the_acl(tmp_path: Path, monkeypatch, caplog) -> None:
    proxy, backend = _client_proxy(tmp_path, "enforce", monkeypatch)
    tls_conn = _FakeSourceSocket(_build_will_connect_packet(will_topic="rr/m/o/hawk-user/someone-else/duid-1"))

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._handle_client(tls_conn, ("127.0.0.1", 4321))

    assert tls_conn.sent == [b"\x20\x02\x00\x05"]
    assert tls_conn.closed is True
    assert backend.connected_to is None and backend.sent == []
    assert "topic ACL blocked WILL topic=rr/m/o/hawk-user/someone-else/duid-1 identity=user:c0ffee" in caplog.text


def test_audit_mode_forwards_connect_with_a_will_outside_the_acl(tmp_path: Path, monkeypatch, caplog) -> None:
    proxy, backend = _client_proxy(tmp_path, "audit", monkeypatch)
    connect = _build_will_connect_packet(will_topic="#", protocol_level=5)
    tls_conn = _FakeSourceSocket(connect)

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._handle_client(tls_conn, ("127.0.0.1", 4321))

    assert backend.sent == [connect]
    assert "topic ACL would block WILL topic=# identity=user:c0ffee" in caplog.text


def test_enforce_mode_accepts_connect_with_a_will_inside_the_acl(tmp_path: Path, monkeypatch, caplog) -> None:
    proxy, backend = _client_proxy(tmp_path, "enforce", monkeypatch)
    connect = _build_will_connect_packet(will_topic="rr/m/i/hawk-user/c0ffee/duid-1")
    tls_conn = _FakeSourceSocket(connect)

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._handle_client(tls_conn, ("127.0.0.1", 4321))

    assert backend.sent == [connect]
    assert "topic ACL" not in caplog.text


def test_enforce_mode_drops_connect_pipelined_with_a_denied_subscribe(tmp_path: Path, monkeypatch, caplog) -> None:
    proxy, backend = _client_proxy(tmp_path, "enforce", monkeypatch)
    connect = _build_connect_packet(client_id="ha-client", username="c0ffee", password="user-pass")
    tls_conn = _FakeSourceSocket(connect + _build_subscribe_packet("#"))

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._handle_client(tls_conn, ("127.0.0.1", 4321))

    # Neither the CONNECT nor the SUBSCRIBE reaches the broker.
    assert backend.sent == []
    assert backend.closed and tls_conn.closed
    assert "topic ACL blocked SUBSCRIBE filters=# identity=user:c0ffee" in caplog.text


def test_audit_mode_forwards_connect_pipelined_with_a_denied_subscribe(tmp_path: Path, monkeypatch, caplog) -> None:
    proxy, backend = _client_proxy(tmp_path, "audit", monkeypatch)
    connect = _build_connect_packet(client_id="ha-client", username="c0ffee", password="user-pass")
    subscribe = _build_subscribe_packet("rr/m/i/hawk-user/c0ffee/duid-1")
    tls_conn = _FakeSourceSocket(connect + subscribe)

    with caplog.at_level(logging.WARNING, logger="test.topic_acl"):
        proxy._handle_client(tls_conn, ("127.0.0.1", 4321))

    assert backend.sent == [connect + subscribe]
    assert "topic ACL would block SUBSCRIBE filters=rr/m/i/hawk-user/c0ffee/duid-1" in caplog.text
