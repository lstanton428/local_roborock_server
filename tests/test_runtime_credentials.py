import json
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


def test_startup_skips_mqtt_password_backfill_when_new_connections_disabled(tmp_path: Path) -> None:
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

    supervisor = ReleaseSupervisor(config=config, paths=paths)

    device = supervisor.runtime_credentials.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == ""
