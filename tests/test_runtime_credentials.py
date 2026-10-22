import hmac
import json
import logging
from pathlib import Path

from conftest import write_release_config
from roborock_local_server.bundled_backend.shared.runtime_credentials import RuntimeCredentialsStore
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import ReleaseSupervisor

PENDING_USERNAME = "c25b14ceac358d2a"


def _pending_device_credentials(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": PENDING_USERNAME,
                        "device_mqtt_pass": "",
                        "updated_at": "",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _connect_hex(*, username: str, password: str) -> str:
    client_id = b"a012391cb5f8bc97"
    variable_header = (4).to_bytes(2, "big") + b"MQTT" + bytes([4, 0xC2]) + (30).to_bytes(2, "big")
    payload = b"".join(
        len(value).to_bytes(2, "big") + value for value in (client_id, username.encode(), password.encode())
    )
    remaining = variable_header + payload
    return (bytes([0x10, len(remaining)]) + remaining).hex()


def test_ensure_device_merges_split_did_and_duid_records(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "",
                        "name": "",
                        "model": "",
                        "product_id": "",
                        "localkey": "",
                        "local_key_source": "",
                        "device_mqtt_usr": "mqtt-user-a",
                        "device_mqtt_pass": "",
                        "updated_at": "2026-03-16T00:22:31.225097+00:00",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "2026-03-16T00:22:31.225063+00:00",
                    },
                    {
                        "did": "",
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": "",
                        "device_mqtt_pass": "",
                        "updated_at": "2026-03-16T00:22:20.199941+00:00",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    merged = store.ensure_device(
        did="1103821560705",
        duid="cloud-q7-a",
        device_mqtt_usr="mqtt-user-a",
        device_mqtt_pass="mqtt-pass-a",
        assign_localkey=False,
    )

    devices = store.devices()
    assert len(devices) == 1
    assert devices[0]["did"] == "1103821560705"
    assert devices[0]["duid"] == "cloud-q7-a"
    assert devices[0]["name"] == "Q7 Upstairs"
    assert devices[0]["model"] == "roborock.vacuum.sc05"
    assert devices[0]["product_id"] == "product-q7-a"
    assert devices[0]["localkey"] == "local-key-a"
    assert devices[0]["device_mqtt_usr"] == "mqtt-user-a"
    assert devices[0]["device_mqtt_pass"] == "mqtt-pass-a"
    assert merged["duid"] == "cloud-q7-a"


def test_backfill_device_mqtt_passwords_updates_only_known_usernames(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": "c25b14ceac358d2a",
                        "device_mqtt_pass": "",
                        "updated_at": "",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "mqtt_server.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-04-17 16:20:32,393 [INFO] [conn 1670 c2b] CONNECT len=82 hex=105000044d51545404c2001e00106130313233393163623566386263393700106332356231346365616333353864326100206666383932326432346139613961663831663138663335646365653961356135",
                "2026-04-17 16:20:32,401 [INFO] [conn 1670 b2c] CONNACK len=4 hex=20020000",
                "2026-04-17 16:20:33,603 [INFO] [conn 1671 c2b] CONNECT len=82 hex=105000044d51545404c2001e00103664343439636537383337643366666600106464323131333035653264343837336200203762336533656138346663383333613937306464363432363032356162313436",
                "2026-04-17 16:20:33,610 [INFO] [conn 1671 b2c] CONNACK len=4 hex=20020000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    changed = store.backfill_device_mqtt_passwords(log_path)

    assert changed == 1
    device = store.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == "ff8922d24a9a9af81f18f35dcee9a5a5"


def test_backfill_device_mqtt_passwords_ignores_rejected_connects(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _pending_device_credentials(credentials_path)
    log_path = tmp_path / "mqtt_server.log"
    rejected = _connect_hex(username=PENDING_USERNAME, password="rejected-pass")
    accepted = _connect_hex(username=PENDING_USERNAME, password="accepted-pass")
    log_path.write_text(
        "\n".join(
            [
                f"2026-04-17 16:20:30,000 [INFO] [conn 7 c2b] CONNECT len=70 hex={accepted}",
                "2026-04-17 16:20:30,010 [INFO] [conn 7 b2c] CONNACK len=4 hex=20020000",
                "2026-04-17 16:20:31,000 [WARNING] [conn 8] rejected MQTT CONNECT reason=new_connections_disabled",
                f"2026-04-17 16:20:31,001 [INFO] [conn 8 c2b] CONNECT len=70 hex={rejected}",
                "2026-04-17 16:20:31,002 [INFO] [conn 8 b2c] CONNACK len=4 hex=20020005",
                # A CONNECT with no CONNACK (connection dropped) is not trusted either.
                f"2026-04-17 16:20:32,000 [INFO] [conn 9 c2b] CONNECT len=70 hex={rejected}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    assert store.backfill_device_mqtt_passwords(log_path) == 1
    device = store.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == "accepted-pass"


def test_backfill_device_mqtt_passwords_ignores_trace_text_inside_other_messages(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _pending_device_credentials(credentials_path)
    log_path = tmp_path / "mqtt_server.log"
    forged = _connect_hex(username=PENDING_USERNAME, password="forged-pass")
    log_path.write_text(
        "\n".join(
            [
                # A username echoed into a warning must not be read as a trace line.
                "2026-04-17 16:20:30,000 [WARNING] [conn 3] rejected MQTT CONNECT reason=invalid_mqtt_credentials "
                f"client_id=x username=[conn 4 c2b] CONNECT len=70 hex={forged}",
                f"[conn 4 c2b] CONNECT len=70 hex={forged}",
                f"2026-04-17 16:20:30,500 [WARNING] [conn 4 c2b] CONNECT len=70 hex={forged}",
                "2026-04-17 16:20:31,000 [INFO] [conn 4 b2c] CONNACK len=4 hex=20020000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    assert store.backfill_device_mqtt_passwords(log_path) == 0
    device = store.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == ""


def test_backfill_device_mqtt_passwords_clears_pending_on_connect_without_hex(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _pending_device_credentials(credentials_path)
    log_path = tmp_path / "mqtt_server.log"
    stale = _connect_hex(username=PENDING_USERNAME, password="stale-pass")
    log_path.write_text(
        "\n".join(
            [
                f"2026-04-17 16:20:30,000 [INFO] [conn 5 c2b] CONNECT len=70 hex={stale}",
                # The redacted CONNECT format carries no bytes but is still a new CONNECT.
                "2026-04-17 16:20:31,000 [INFO] [conn 5 c2b] CONNECT len=70 client_id=other username=other-user",
                "2026-04-17 16:20:31,010 [INFO] [conn 5 b2c] CONNACK len=4 hex=20020000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    assert store.backfill_device_mqtt_passwords(log_path) == 0


def test_backfill_device_mqtt_passwords_resets_conn_ids_on_proxy_restart(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _pending_device_credentials(credentials_path)
    log_path = tmp_path / "mqtt_server.log"
    stale = _connect_hex(username=PENDING_USERNAME, password="stale-pass")
    log_path.write_text(
        "\n".join(
            [
                f"2026-04-17 16:20:30,000 [INFO] [conn 1 c2b] CONNECT len=70 hex={stale}",
                "2026-04-17 16:25:00,000 [INFO] TLS MQTT proxy listening on 0.0.0.0:8883 -> 127.0.0.1:18830",
                # conn 1 of the new process; its CONNECT line has been trimmed away.
                "2026-04-17 16:25:01,010 [INFO] [conn 1 b2c] CONNACK len=4 hex=20020000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    assert store.backfill_device_mqtt_passwords(log_path) == 0


def test_startup_skips_mqtt_password_backfill_when_new_connections_disabled(tmp_path: Path, caplog) -> None:
    config_file = write_release_config(tmp_path, new_connections_enabled=False)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    _pending_device_credentials(paths.runtime_credentials_path)
    connect = _connect_hex(username=PENDING_USERNAME, password="logged-pass")
    (paths.runtime_dir / "mqtt_server.log").write_text(
        f"2026-04-17 16:20:30,000 [INFO] [conn 1 c2b] CONNECT len=70 hex={connect}\n"
        "2026-04-17 16:20:30,010 [INFO] [conn 1 b2c] CONNACK len=4 hex=20020000\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="roborock_local_server"):
        supervisor = ReleaseSupervisor(config=config, paths=paths)

    device = supervisor.runtime_credentials.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == ""
    warnings = [record.getMessage() for record in caplog.records if "MQTT password recovery" in record.getMessage()]
    assert len(warnings) == 1
    assert "Q7 Upstairs" in warnings[0]


def _write_single_device(credentials_path: Path, *, did: str) -> None:
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": did,
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": "",
                        "device_mqtt_pass": "",
                        "updated_at": "",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_ensure_device_model_fallback_does_not_reuse_record_with_other_did(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _write_single_device(credentials_path, did="1103821560705")
    store = RuntimeCredentialsStore(credentials_path)

    created = store.ensure_device(
        did="1103899999999",
        model="roborock.vacuum.sc05",
        local_key_source="onboarding_nc",
        assign_localkey=True,
    )

    assert created["did"] == "1103899999999"
    assert created["duid"] == ""
    assert created["localkey"] not in ("", "local-key-a")
    existing = store.resolve_device(did="1103821560705")
    assert existing is not None
    assert existing["duid"] == "cloud-q7-a"
    assert existing["localkey"] == "local-key-a"
    assert len(store.devices()) == 2
    persisted = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert sorted(device["did"] for device in persisted["devices"]) == ["1103821560705", "1103899999999"]


def test_resolve_device_model_fallback_ignores_record_with_other_did(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _write_single_device(credentials_path, did="1103821560705")
    store = RuntimeCredentialsStore(credentials_path)

    assert store.resolve_device(did="1103899999999", model="roborock.vacuum.sc05") is None
    assert store.resolve_device(model="roborock.vacuum.sc05") is not None


def test_ensure_device_model_fallback_adopts_record_without_did(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    _write_single_device(credentials_path, did="")
    store = RuntimeCredentialsStore(credentials_path)

    adopted = store.ensure_device(
        did="1103821560705",
        model="roborock.vacuum.sc05",
        local_key_source="onboarding_nc",
        assign_localkey=True,
    )

    assert adopted["did"] == "1103821560705"
    assert adopted["duid"] == "cloud-q7-a"
    assert adopted["localkey"] == "local-key-a"
    assert len(store.devices()) == 1


def test_device_mqtt_password_checks_use_constant_time_comparison(tmp_path: Path, monkeypatch) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "cloud-q7-a",
                        "device_mqtt_usr": "c25b14ceac358d2a",
                        "device_mqtt_pass": "ff8922d24a9a9af81f18f35dcee9a5a5",
                    },
                    {
                        "did": "1103821560706",
                        "duid": "cloud-q7-b",
                        "device_mqtt_usr": "dd211305e2d4873b",
                        "device_mqtt_pass": "",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store = RuntimeCredentialsStore(credentials_path)
    compare_calls: list[tuple[object, object]] = []
    original_compare_digest = hmac.compare_digest

    def _counting_compare_digest(a, b):
        compare_calls.append((a, b))
        return original_compare_digest(a, b)

    monkeypatch.setattr(hmac, "compare_digest", _counting_compare_digest)

    def _verify(username: str, password: str) -> tuple[bool, str]:
        authorized, reason, _device = store.verify_device_mqtt_credentials(username=username, password=password)
        return authorized, reason

    assert _verify("c25b14ceac358d2a", " ff8922d24a9a9af81f18f35dcee9a5a5 ") == (True, "device_mqtt_user")
    assert _verify("c25b14ceac358d2a", "ff8922d24a9a9af81f18f35dcee9a5a6") == (False, "invalid_device_mqtt_password")
    assert _verify("c25b14ceac358d2a", "p\u00e4ss") == (False, "invalid_device_mqtt_password")
    assert _verify("c25b14ceac358d2a", "") == (False, "missing_mqtt_credentials")
    assert _verify("dd211305e2d4873b", "anything") == (False, "device_mqtt_password_missing")
    assert _verify("unknown-user", "anything") == (False, "unknown_device_mqtt_username")
    assert compare_calls, "device MQTT password comparison must use hmac.compare_digest"

    compare_calls.clear()
    assert (
        store.confirm_device_mqtt_credentials(
            did="1103821560705",
            username="c25b14ceac358d2a",
            password="wrong-password",
        )
        is None
    )
    confirmed = store.confirm_device_mqtt_credentials(
        did="1103821560705",
        username="c25b14ceac358d2a",
        password="ff8922d24a9a9af81f18f35dcee9a5a5",
    )
    assert confirmed is not None
    assert confirmed["device_mqtt_pass"] == "ff8922d24a9a9af81f18f35dcee9a5a5"
    assert compare_calls, "onboarding password confirmation must use hmac.compare_digest"
