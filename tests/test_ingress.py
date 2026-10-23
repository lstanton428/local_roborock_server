import json
import logging

from fastapi.testclient import TestClient

from conftest import write_release_config
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.ingress import HA_INGRESS_PROXY, admin_dashboard_url, build_ingress_app
from roborock_local_server.server import ReleaseSupervisor

ADMIN_URL = "https://api-roborock.example.com:555/admin"


def _panel(status: dict | None = None, changes: list | None = None) -> TestClient:
    app = build_ingress_app(
        admin_url=ADMIN_URL,
        status=lambda: status or {"overall_ok": True},
        set_new_connections_enabled=(changes if changes is not None else []).append,
    )
    return TestClient(app, client=(HA_INGRESS_PROXY, 50000))


def test_admin_dashboard_url_uses_the_stack_hostname_and_public_port() -> None:
    assert admin_dashboard_url("api-roborock.example.com", 555) == "https://api-roborock.example.com:555/admin"
    assert admin_dashboard_url("api-roborock.example.com", 443) == "https://api-roborock.example.com/admin"


def test_panel_links_to_the_admin_dashboard_in_a_new_window() -> None:
    response = _panel().get("/")

    assert response.status_code == 200
    # The Home Assistant apps open new-window links in the default browser.
    assert f'<a id="admin" class="button" href="{ADMIN_URL}" target="_blank" rel="noopener noreferrer">' in response.text
    assert "<script>" not in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
    assert response.headers["cache-control"] == "no-store"
    for asset, media_type in (("panel.js", "text/javascript"), ("panel.css", "text/css")):
        assert _panel().get(asset).headers["content-type"].startswith(media_type)


def test_panel_answers_only_the_home_assistant_ingress_proxy() -> None:
    changes: list[bool] = []
    app = build_ingress_app(admin_url=ADMIN_URL, status=lambda: {"overall_ok": True}, set_new_connections_enabled=changes.append)
    for client_host in ("172.30.32.1", "172.30.33.5", "127.0.0.1", "192.168.1.20"):
        client = TestClient(app, client=(client_host, 50000))
        for response in (
            client.get("/"),
            client.get("/api/status"),
            client.post("/api/new-connections", json={"enabled": True}),
        ):
            assert response.status_code == 403
            assert "api-roborock" not in response.text
    assert changes == []


def test_panel_opens_and_closes_new_connections(caplog) -> None:
    changes: list[bool] = []
    client = _panel({"overall_ok": True, "new_connections": {"enabled": True}}, changes)

    with caplog.at_level(logging.INFO, logger="roborock_local_server.ingress"):
        opened = client.post("/api/new-connections", json={"enabled": True}, headers={"X-Remote-User-Display-Name": "Levi"})
        closed = client.post("/api/new-connections", json={"enabled": False})

    assert opened.status_code == closed.status_code == 200
    assert opened.json() == {"overall_ok": True, "new_connections": {"enabled": True}, "controls": True}
    assert changes == [True, False]
    assert "New connections opened from Home Assistant by Levi" in caplog.messages


def test_panel_rejects_new_connection_changes_that_are_not_json_booleans() -> None:
    changes: list[bool] = []
    client = _panel(changes=changes)

    # A form post is what a cross-site page could send without a CORS preflight.
    assert client.post("/api/new-connections", data={"enabled": "true"}).status_code == 415
    assert client.post("/api/new-connections", content="{", headers={"Content-Type": "application/json"}).status_code == 400
    for body in ({"enabled": "true"}, {"enabled": 1}, {}, [True]):
        assert client.post("/api/new-connections", json=body).status_code == 400
    assert changes == []


def test_panel_is_read_only_without_new_connection_controls() -> None:
    app = build_ingress_app(admin_url=ADMIN_URL, status=lambda: {"overall_ok": True}, set_new_connections_enabled=None)
    client = TestClient(app, client=(HA_INGRESS_PROXY, 50000))

    assert client.get("/api/status").json() == {"overall_ok": True, "controls": False}
    for enabled in (True, False):
        assert client.post("/api/new-connections", json={"enabled": enabled}).status_code == 403


def test_ingress_status_reports_new_connections_without_secrets(tmp_path) -> None:
    config_file = write_release_config(tmp_path, listener_mode="external_tls")
    config = load_config(config_file)
    supervisor = ReleaseSupervisor(config=config, paths=resolve_paths(config_file, config))

    status = supervisor.ingress_status()
    assert status["new_connections"]["enabled"] is True
    assert status["new_connections"]["turned_on"] is True
    assert status["certificate"] is None
    assert isinstance(status["vacuums"], list)
    serialized = json.dumps(status)
    assert config.admin.session_secret not in serialized
    assert "protocol_sync_secret" not in serialized and "protocol_sessions" not in serialized

    supervisor.set_new_connections_enabled(False)
    status = supervisor.ingress_status()
    assert status["new_connections"]["enabled"] is False
    assert status["new_connections"]["turned_on"] is False
