"""State-changing admin routes reject cross-origin and non-JSON browser requests."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from conftest import write_release_config
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import ReleaseSupervisor

PASSWORD = "correct horse battery staple"


def _client(tmp_path: Path, **config_kwargs) -> tuple[ReleaseSupervisor, TestClient]:
    config_file = write_release_config(tmp_path, **config_kwargs)
    config = load_config(config_file)
    supervisor = ReleaseSupervisor(config=config, paths=resolve_paths(config_file, config))
    return supervisor, TestClient(supervisor.app)


def _login(client: TestClient) -> None:
    assert client.post("/admin/api/login", json={"password": PASSWORD}).status_code == 200


def test_dashboard_style_requests_still_work(tmp_path: Path) -> None:
    supervisor, client = _client(tmp_path)
    same_origin = {"Origin": "http://testserver"}
    login = client.post("/admin/api/login", json={"password": PASSWORD}, headers=same_origin)
    assert login.status_code == 200
    toggled = client.post("/admin/api/auth", json={"new_connections_enabled": False}, headers=same_origin)
    assert toggled.status_code == 200
    assert toggled.json()["new_connections_enabled"] is False
    assert client.post("/admin/api/logout", headers=same_origin).status_code == 200


def test_onboarding_script_style_requests_still_work(tmp_path: Path) -> None:
    # start_onboarding.py sends JSON with Content-Type and no Origin header.
    _supervisor, client = _client(tmp_path)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    assert client.post("/admin/api/login", content=f'{{"password":"{PASSWORD}"}}', headers=headers).status_code == 200
    started = client.post("/admin/api/onboarding/sessions", content='{"new_vacuum":true}', headers=headers)
    assert started.status_code == 200
    session_id = started.json()["session_id"]
    deleted = client.delete(f"/admin/api/onboarding/sessions/{session_id}", headers={"Accept": "application/json"})
    assert deleted.status_code == 200


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
def test_json_routes_require_json_content_type(tmp_path: Path, content_type: str | None) -> None:
    supervisor, client = _client(tmp_path)
    headers = {"Content-Type": content_type} if content_type else {}
    login = client.post("/admin/api/login", content=f'{{"password":"{PASSWORD}"}}', headers=headers)
    assert login.status_code == 403
    assert supervisor.session_manager.cookie_name not in login.cookies

    _login(client)
    toggled = client.post("/admin/api/auth", content='{"new_connections_enabled":false}', headers=headers)
    assert toggled.status_code == 403
    assert supervisor.new_connections_enabled() is True
    started = client.post("/admin/api/onboarding/sessions", content='{"new_vacuum":true}', headers=headers)
    assert started.status_code == 403
    assert supervisor.runtime_state.onboarding_session_snapshot()["active"] is False


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://testserver.evil.example", "http://testserver:8080", "https://testserver", "null"],
)
def test_cross_origin_requests_are_rejected(tmp_path: Path, origin: str) -> None:
    supervisor, client = _client(tmp_path)
    _login(client)
    started = client.post("/admin/api/onboarding/sessions", json={"new_vacuum": True})
    session_id = started.json()["session_id"]
    headers = {"Origin": origin}

    assert client.post("/admin/api/login", json={"password": PASSWORD}, headers=headers).status_code == 403
    assert client.post(
        "/admin/api/auth", json={"new_connections_enabled": False}, headers=headers
    ).status_code == 403
    assert supervisor.new_connections_enabled() is True
    assert client.delete(f"/admin/api/onboarding/sessions/{session_id}", headers=headers).status_code == 403
    assert supervisor.runtime_state.onboarding_session_snapshot()["active"] is True
    assert client.delete("/admin/api/auth/sessions/a/b", headers=headers).status_code == 403
    assert client.post(
        "/admin/api/cloud/request-code", json={"email": "user@example.com"}, headers=headers
    ).status_code == 403
    assert client.post("/admin/api/logout", headers=headers).status_code == 403


def test_external_tls_expects_https_origin(tmp_path: Path) -> None:
    # The TLS-terminating proxy forwards plain HTTP but preserves the Host header.
    _supervisor, client = _client(tmp_path, listener_mode="external_tls")
    host = {"Host": "roborock.example.com"}
    accepted = client.post(
        "/admin/api/login",
        json={"password": PASSWORD},
        headers={**host, "Origin": "https://roborock.example.com"},
    )
    assert accepted.status_code == 200
    rejected = client.post(
        "/admin/api/login",
        json={"password": PASSWORD},
        headers={**host, "Origin": "http://roborock.example.com"},
    )
    assert rejected.status_code == 403
