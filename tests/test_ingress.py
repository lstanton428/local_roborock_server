import base64
import hashlib
import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from roborock_local_server.ingress import HA_INGRESS_PROXY, admin_dashboard_url, build_ingress_app


def test_admin_dashboard_url_uses_the_stack_hostname_and_public_port() -> None:
    assert admin_dashboard_url("api-roborock.example.com", 555) == "https://api-roborock.example.com:555/admin"
    assert admin_dashboard_url("api-roborock.example.com", 443) == "https://api-roborock.example.com/admin"


def test_ingress_page_sends_the_top_window_to_the_admin_dashboard() -> None:
    app = build_ingress_app(admin_url="https://api-roborock.example.com:555/admin")
    response = TestClient(app, client=(HA_INGRESS_PROXY, 50000)).get("/")

    assert response.status_code == 200
    assert (
        '<a id="admin" href="https://api-roborock.example.com:555/admin" target="_blank" rel="noopener noreferrer">'
        in response.text
    )
    [script] = re.findall(r"<script>(.*?)</script>", response.text)
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


# Runs the page script against fake windows: a browser tab, the Home Assistant
# iOS/macOS app (webkit externalBus) and Android app (externalApp), and a top
# window the frame may not read.
_SCRIPT_HARNESS = r"""
function run(top) {
  const calls = [];
  const window = { top: top, open: (url, target) => calls.push(["open", url, target]) };
  top.location = { set href(url) { calls.push(["top", url]); } };
  const document = { getElementById: () => ({ href: "https://api-roborock.example.com:555/admin" }) };
  new Function("window", "document", SCRIPT)(window, document);
  return calls;
}
const blocked = new Proxy({}, {
  get(target, key) { if (key === "location") return target.location; throw new Error("cross-origin"); },
  set(target, key, value) { target[key] = value; return true; },
});
console.log(JSON.stringify({
  browser: run({}),
  apple_app: run({ webkit: { messageHandlers: { externalBus: {} } } }),
  android_app: run({ externalApp: {} }),
  cross_origin: run(blocked),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page script")
def test_ingress_script_opens_the_default_browser_from_the_home_assistant_apps() -> None:
    from roborock_local_server.ingress import _REDIRECT_SCRIPT

    harness = _SCRIPT_HARNESS.replace("SCRIPT", json.dumps(_REDIRECT_SCRIPT))
    result = json.loads(subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout)
    url = "https://api-roborock.example.com:555/admin"
    # The apps open only new-window requests in the default browser.
    assert result["apple_app"] == [["open", url, "_blank"]]
    assert result["android_app"] == [["open", url, "_blank"]]
    assert result["browser"] == [["top", url]]
    assert result["cross_origin"] == [["top", url]]
