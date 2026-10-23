import base64
import hashlib
import re

from fastapi.testclient import TestClient

from roborock_local_server.ingress import HA_INGRESS_PROXY, admin_dashboard_url, build_ingress_app


def test_admin_dashboard_url_uses_the_stack_hostname_and_public_port() -> None:
    assert admin_dashboard_url("api-roborock.example.com", 555) == "https://api-roborock.example.com:555/admin"
    assert admin_dashboard_url("api-roborock.example.com", 443) == "https://api-roborock.example.com/admin"


def test_ingress_page_sends_the_top_window_to_the_admin_dashboard() -> None:
    app = build_ingress_app(admin_url="https://api-roborock.example.com:555/admin")
    response = TestClient(app, client=(HA_INGRESS_PROXY, 50000)).get("/")

    assert response.status_code == 200
    assert '<a id="admin" href="https://api-roborock.example.com:555/admin" target="_top">' in response.text
    [script] = re.findall(r"<script>(.*?)</script>", response.text)
    assert "window.top.location" in script
    # The inline script is the only one the page's CSP allows.
    script_hash = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    csp = response.headers["content-security-policy"]
    assert f"script-src 'sha256-{script_hash}'" in csp
    assert "default-src 'none'" in csp
    assert response.headers["cache-control"] == "no-store"


def test_ingress_page_answers_only_the_home_assistant_ingress_proxy() -> None:
    app = build_ingress_app(admin_url="https://api-roborock.example.com:555/admin")
    for client_host in ("172.30.32.1", "172.30.33.5", "127.0.0.1", "192.168.1.20"):
        response = TestClient(app, client=(client_host, 50000)).get("/")
        assert response.status_code == 403
        assert "api-roborock" not in response.text
