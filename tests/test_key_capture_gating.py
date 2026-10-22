"""Key-recovery samples are only captured while a vacuum could be pairing."""

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import write_release_config
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import ReleaseSupervisor

KNOWN_DID = "1103821560705"
NEW_DID = "1103835404427"
SIGNATURE = base64.b64encode(b"\x01" * 256).decode("ascii")


def _supervisor(tmp_path: Path, monkeypatch, *, new_connections_enabled: bool = True) -> ReleaseSupervisor:
    config_file = write_release_config(tmp_path, new_connections_enabled=new_connections_enabled)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.runtime_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": KNOWN_DID,
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "localkey": "local-key-a",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    supervisor = ReleaseSupervisor(config=config, paths=paths)
    key_cache = supervisor.context.device_key_cache()
    assert key_cache is not None
    monkeypatch.setattr(key_cache, "maybe_recover_async", lambda did: None)
    return supervisor


def _captured_dids(supervisor: ReleaseSupervisor) -> list[str]:
    return supervisor.context.device_key_cache().get_known_dids()


def _signed_region(client: TestClient, did: str) -> None:
    client.get(f"/region?did={did}&a=1&signature={SIGNATURE}")
    client.get(
        f"/region?did={did}",
        headers={"v": "v2", "s": base64.b64encode(b"\x02" * 512).decode(), "n": "nonce", "t": "1"},
    )


def test_unknown_did_is_captured_while_new_connections_are_enabled(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    _signed_region(TestClient(supervisor.app), NEW_DID)
    assert _captured_dids(supervisor) == [NEW_DID]


def test_known_did_is_not_captured_without_onboarding_session(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    _signed_region(TestClient(supervisor.app), KNOWN_DID)
    assert _captured_dids(supervisor) == []


def test_nothing_is_captured_while_new_connections_are_disabled(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch, new_connections_enabled=False)
    client = TestClient(supervisor.app)
    _signed_region(client, NEW_DID)
    _signed_region(client, KNOWN_DID)
    assert _captured_dids(supervisor) == []


def test_known_did_is_captured_during_onboarding_session(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    supervisor.runtime_state.start_onboarding_session(
        target_duid="cloud-q7-a",
        target_name="Q7 Upstairs",
        target_did=KNOWN_DID,
    )
    _signed_region(TestClient(supervisor.app), KNOWN_DID)
    assert _captured_dids(supervisor) == [KNOWN_DID]


def test_onboarding_target_is_captured_when_did_limit_is_full(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path, monkeypatch)
    client = TestClient(supervisor.app)
    for index in range(10):
        client.get(f"/region?did=99{index:011d}&a=1&signature={SIGNATURE}")
    assert len(_captured_dids(supervisor)) == 8

    supervisor.runtime_state.start_onboarding_session(
        target_duid="cloud-q7-a",
        target_name="Q7 Upstairs",
        target_did=KNOWN_DID,
    )
    client.get(f"/region?a=1&signature={SIGNATURE}")
    assert KNOWN_DID in _captured_dids(supervisor)
