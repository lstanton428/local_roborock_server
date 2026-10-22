import asyncio
import errno
import json
import os
from pathlib import Path
import stat

import pytest

from conftest import write_release_config
from roborock_local_server import turn_relay
from roborock_local_server.bundled_backend.shared.device_key_recovery import DeviceKeyCache
from roborock_local_server.bundled_backend.shared.inventory_io import atomic_write_inventory
from roborock_local_server.bundled_backend.shared.io_utils import atomic_write_text, restrict_to_owner
from roborock_local_server.bundled_backend.shared.protocol_auth import ProtocolAuthStore
from roborock_local_server.bundled_backend.shared.runtime_credentials import RuntimeCredentialsStore
from roborock_local_server.config import TurnConfig, load_config, resolve_paths
from roborock_local_server.ha_addon import write_config_from_home_assistant_options
from roborock_local_server.server import ReleaseSupervisor, run_server, secret_file_paths

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def permissive_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_atomic_write_text_creates_and_replaces_owner_only_files(tmp_path: Path) -> None:
    created = tmp_path / "secret.json"
    atomic_write_text(created, '{"k": 1}\n')
    assert created.read_text(encoding="utf-8") == '{"k": 1}\n'
    assert _mode(created) == 0o600

    existing = tmp_path / "existing.json"
    existing.write_text("old", encoding="utf-8")
    existing.chmod(0o644)
    atomic_write_text(existing, "new")
    assert existing.read_text(encoding="utf-8") == "new"
    assert _mode(existing) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == ["existing.json", "secret.json"]


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_atomic_write_text_failure_leaves_old_file_intact(tmp_path: Path, monkeypatch, failing) -> None:
    path = tmp_path / "runtime_credentials.json"
    path.write_text('{"devices": ["original"]}\n', encoding="utf-8")
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(os, failing, fail)
    with pytest.raises(OSError, match="disk failure"):
        atomic_write_text(path, '{"devices": []}\n')
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_text_rewrites_busy_mount_point_in_place(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)

    def busy(*args, **kwargs):
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "replace", busy)
    atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert _mode(path) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_restrict_to_owner_only_changes_group_or_world_accessible_files(tmp_path: Path) -> None:
    readable = tmp_path / "readable"
    readable.write_text("x")
    readable.chmod(0o644)
    executable = tmp_path / "executable"
    executable.write_text("x")
    executable.chmod(0o755)
    private = tmp_path / "private"
    private.write_text("x")
    private.chmod(0o600)

    directory = tmp_path / "directory"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    changed = restrict_to_owner([readable, executable, private, tmp_path / "missing", directory])

    assert changed == [readable, executable]
    assert _mode(readable) == 0o600
    assert _mode(executable) == 0o700
    assert _mode(private) == 0o600
    assert _mode(directory) == 0o755


def test_secret_stores_write_owner_only_files(tmp_path: Path) -> None:
    credentials = RuntimeCredentialsStore(tmp_path / "runtime_credentials.json")
    credentials.update_base(api_host="api.example.com")
    assert _mode(credentials.path) == 0o600

    sessions_path = tmp_path / "protocol_auth_sessions.json"
    store = ProtocolAuthStore(tmp_path / "snapshot.json", session_store_path=sessions_path)
    store._persist_session_records_locked([{"token": "t"}])
    assert json.loads(sessions_path.read_text())["sessions"] == [{"token": "t"}]
    assert _mode(sessions_path) == 0o600

    key_state = tmp_path / "device_key_state.json"
    DeviceKeyCache(key_state)._save()
    assert json.loads(key_state.read_text()) == {"devices": {}}
    assert _mode(key_state) == 0o600

    inventory = tmp_path / "web_api_inventory.json"
    atomic_write_inventory(inventory, {"devices": []})
    assert _mode(inventory) == 0o600


def test_turn_relay_config_is_never_world_readable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(turn_relay.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("192.0.2.1", 3478))])
    modes = []

    def popen(args, **kwargs):
        modes.append(_mode(Path(args[-1])))
        raise FileNotFoundError("coturn unavailable")

    monkeypatch.setattr(turn_relay.subprocess, "Popen", popen)
    relay = turn_relay.EmbeddedTurnRelay(
        TurnConfig(mode="provided", host="turn.example.com", username="user", password="secret", realm="example.com"),
        tmp_path,
    )
    with pytest.raises(FileNotFoundError):
        relay.start()
    assert modes == [0o600]


def test_home_assistant_config_and_secrets_are_owner_only(tmp_path: Path) -> None:
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps({
        "stack_fqdn": "api-roborock.example.com",
        "tls_mode": "cloudflare_acme",
        "tls_base_domain": "example.com",
        "tls_email": "acme@example.com",
        "acme_server": "zerossl",
        "cloudflare_token": "cloudflare-token-123",
        "admin_password": "secret",
        "protocol_login_email": "user@example.com",
        "protocol_login_pin": "654321",
    }))
    config_path = tmp_path / "config.toml"
    token_path = tmp_path / "secrets" / "cloudflare_token"

    write_config_from_home_assistant_options(
        options_path=options_path, config_path=config_path, cloudflare_token_path=token_path,
    )

    assert _mode(config_path) == 0o600
    assert _mode(token_path) == 0o600


def test_configure_writes_owner_only_config(tmp_path: Path) -> None:
    from test_configure import _answers
    from roborock_local_server.configure import write_config_setup

    result = write_config_setup(config_file=tmp_path / "config.toml", answers=_answers())

    assert _mode(result.config_file) == 0o600
    assert _mode(result.cloudflare_token_file) == 0o600


def test_admin_setting_rewrite_keeps_config_owner_only(tmp_path: Path) -> None:
    config_file = write_release_config(tmp_path)
    config_file.chmod(0o644)
    config = load_config(config_file)
    supervisor = ReleaseSupervisor(config=config, paths=resolve_paths(config_file, config))

    supervisor.set_protocol_auth_enabled(False)

    assert "protocol_auth_enabled = false" in config_file.read_text(encoding="utf-8")
    assert _mode(config_file) == 0o600


def test_run_server_restricts_umask_and_existing_secret_files(tmp_path: Path, monkeypatch) -> None:
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        config_file: None,
        paths.runtime_credentials_path: '{"schema_version": 2, "devices": []}\n',
        paths.protocol_auth_sessions_path: '{"version": 1, "sessions": []}\n',
        paths.device_key_state_path: '{"devices":{}}\n',
        paths.inventory_path: "{}\n",
        paths.http_jsonl_path: "",
    }
    for path, contents in legacy.items():
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        path.chmod(0o644)
    config_before = config_file.read_bytes()
    seen = {}

    async def serve_forever(self):
        seen["umask"] = os.umask(0o077)
        return 0

    monkeypatch.setattr(ReleaseSupervisor, "serve_forever", serve_forever)

    assert asyncio.run(run_server(config_file=config_file, enable_standalone_admin=False)) == 0

    assert seen["umask"] == 0o077
    for path in legacy:
        assert _mode(path) == 0o600, path
    assert config_file.read_bytes() == config_before
    assert set(legacy) <= set(secret_file_paths(paths))


def test_secret_file_paths_cover_turn_and_tls_secrets(tmp_path: Path) -> None:
    config_file = write_release_config(tmp_path)
    paths = resolve_paths(config_file, load_config(config_file))

    listed = set(secret_file_paths(paths))

    assert {
        paths.key_file,
        paths.cloudflare_token_file,
        paths.acme_eab_kid_file,
        paths.acme_eab_hmac_key_file,
        paths.cloud_snapshot_path,
        paths.state_dir / "turn_password",
        paths.state_dir / "turn_relay" / "turnserver.conf",
    } <= listed


def test_cloud_import_snapshot_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    import test_cloud

    monkeypatch.setattr(test_cloud.cloud_module, "RoborockApiClient", test_cloud._FakeRoborockApiClientV1Fallback)
    test_cloud._patch_backend_calls(monkeypatch)
    manager = test_cloud._make_manager(tmp_path)

    requested = asyncio.run(manager.request_code(email="user@example.com"))
    assert asyncio.run(manager.submit_code(session_id=requested["session_id"], code="123456"))["success"]

    assert _mode(tmp_path / "cloud_snapshot.json") == 0o600
    assert _mode(tmp_path / "inventory.json") == 0o600
