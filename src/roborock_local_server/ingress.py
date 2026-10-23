"""Home Assistant ingress panel for the add-on.

Home Assistant builds an add-on's webui link from the hostname the browser used
to reach Home Assistant, which is never the stack hostname the certificate
covers. Ingress opens this panel instead. It shows the server's health, lets a
Home Assistant admin open or close new connections, and links to the full admin
dashboard on the stack hostname. Everything else stays on that dashboard, behind
its own password.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

# Home Assistant's ingress proxy always connects from this address.
HA_INGRESS_PROXY = "172.30.32.2"

LOG = logging.getLogger(__name__)

# The panel is framed by Home Assistant and reached under its ingress path, so
# 'self' is Home Assistant's origin and every URL below is relative.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roborock Local Server</title>
<link rel="stylesheet" href="panel.css">
<script src="panel.js" defer></script>
</head>
<body>
<main>
  <header>
    <h1>Roborock Local Server</h1>
    <span id="health" class="badge">Loading…</span>
  </header>

  <section class="card">
    <h2>New connections</h2>
    <p class="hint">Needed when a phone app, Home Assistant or a vacuum signs in or pairs for the first time.</p>
    <p id="nc-state" class="state">Loading…</p>
    <div class="actions">
      <button id="nc-open" type="button" class="primary">Open</button>
      <button id="nc-close" type="button">Close now</button>
    </div>
    <p id="nc-error" class="error" hidden></p>
  </section>

  <section class="card">
    <h2>Vacuums</h2>
    <ul id="vacuums" class="list"><li class="muted">Loading…</li></ul>
  </section>

  <section class="card">
    <h2>Server</h2>
    <ul class="list">
      <li><span>Certificate</span><span id="cert" class="muted">Loading…</span></li>
      <li id="services-row" hidden><span>Not running</span><span id="services" class="bad"></span></li>
    </ul>
  </section>

  <a id="admin" class="button" href="{admin_url}" target="_blank" rel="noopener noreferrer">Open admin dashboard</a>
  <p class="hint">Cloud import, onboarding and app sessions are in the admin dashboard.</p>
</main>
</body>
</html>
"""

_CSS = """
:root { color-scheme: light dark; --bg: #f5f5f5; --card: #ffffff; --text: #1f1f1f; --muted: #6b6b6b;
  --line: #e3e3e3; --accent: #03a9f4; --accent-text: #ffffff; --good: #2e7d32; --bad: #c62828; --warn: #b26a00; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #111111; --card: #1c1c1c; --text: #e1e1e1; --muted: #9b9b9b; --line: #2c2c2c;
    --good: #66bb6a; --bad: #ef5350; --warn: #ffa726; }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 560px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 4px 0 16px; }
h1 { font-size: 20px; font-weight: 500; margin: 0; }
h2 { font-size: 15px; font-weight: 600; margin: 0 0 6px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 12px; }
.hint, .muted { color: var(--muted); }
.hint { font-size: 13px; margin: 0 0 10px; }
.state { font-size: 17px; margin: 0 0 12px; }
.badge { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); white-space: nowrap; }
.good { color: var(--good); }
.bad { color: var(--bad); }
.warn { color: var(--warn); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button, .button { font: inherit; font-weight: 500; border-radius: 8px; padding: 8px 16px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); text-decoration: none; }
button.primary, .button { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
button:disabled { opacity: 0.5; cursor: default; }
.button { display: block; text-align: center; margin: 4px 0 8px; }
.list { list-style: none; margin: 0; padding: 0; }
.list li { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-top: 1px solid var(--line); }
.list li:first-child { border-top: none; }
.error { color: var(--bad); margin: 10px 0 0; }
"""

_JS = """
"use strict";
let status = null;
let fetchedAt = 0;

function text(id, value, className) {
  const element = document.getElementById(id);
  element.textContent = value;
  if (className !== undefined) element.className = className;
}

function remainingSeconds() {
  const seconds = status.new_connections.seconds_remaining;
  if (seconds === null || seconds === undefined) return null;
  return Math.max(0, seconds - Math.floor((Date.now() - fetchedAt) / 1000));
}

function windowLabel(minutes) {
  return minutes > 0 ? "Open for " + minutes + " min" : "Open";
}

function renderNewConnections() {
  const nc = status.new_connections;
  const remaining = remainingSeconds();
  let state;
  let className = "state";
  if (!nc.turned_on) {
    state = "Closed (turned off)";
    className += " bad";
  } else if (!nc.enabled || remaining === 0) {
    state = "Closed (the window ended)";
    className += " bad";
  } else if (remaining === null) {
    state = "Open until closed";
    className += " good";
  } else {
    const minutes = Math.floor(remaining / 60);
    const seconds = String(remaining % 60).padStart(2, "0");
    state = "Open, closes in " + minutes + ":" + seconds;
    className += " good";
  }
  text("nc-state", state, className);
  const open = nc.turned_on && nc.enabled && remaining !== 0;
  document.getElementById("nc-open").textContent = open && remaining !== null ? "Restart window" : windowLabel(nc.window_minutes);
  document.getElementById("nc-open").disabled = open && remaining === null;
  document.getElementById("nc-close").disabled = !open;
}

function render() {
  if (status.overall_ok) text("health", "Healthy", "badge good");
  else text("health", "Needs attention", "badge bad");
  renderNewConnections();

  const list = document.getElementById("vacuums");
  list.replaceChildren();
  if (!status.vacuums.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "No vacuums yet";
    list.appendChild(item);
  }
  for (const vacuum of status.vacuums) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = vacuum.model ? vacuum.name + " (" + vacuum.model + ")" : vacuum.name;
    const state = document.createElement("span");
    state.textContent = vacuum.connected ? "Connected" : "Offline";
    state.className = vacuum.connected ? "good" : "muted";
    item.append(name, state);
    list.appendChild(item);
  }

  const certificate = status.certificate;
  if (certificate) {
    const expires = new Date(certificate.not_after);
    const days = Math.floor((expires - Date.now()) / 86400000);
    const className = days < 0 ? "bad" : days < 14 ? "warn" : "muted";
    text("cert", (days < 0 ? "Expired " : "Expires ") + expires.toLocaleDateString() + " (" + days + " days)", className);
  } else {
    text("cert", "Managed outside the add-on", "muted");
  }
  document.getElementById("services-row").hidden = !status.services_down.length;
  text("services", status.services_down.join(", "));
}

function showError(message) {
  const element = document.getElementById("nc-error");
  element.textContent = message;
  element.hidden = !message;
}

async function refresh() {
  try {
    const response = await fetch("api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Status request failed (" + response.status + ")");
    status = await response.json();
    fetchedAt = Date.now();
    render();
  } catch (error) {
    text("health", "Unreachable", "badge bad");
  }
}

async function setNewConnections(enabled) {
  showError("");
  for (const id of ["nc-open", "nc-close"]) document.getElementById(id).disabled = true;
  try {
    const response = await fetch("api/new-connections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enabled }),
    });
    if (!response.ok) throw new Error("The server refused the change (" + response.status + ")");
    status = await response.json();
    fetchedAt = Date.now();
    render();
  } catch (error) {
    showError(error.message);
    if (status) render();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nc-open").addEventListener("click", () => setNewConnections(true));
  document.getElementById("nc-close").addEventListener("click", () => setNewConnections(false));
  refresh();
  setInterval(refresh, 15000);
  setInterval(() => { if (status) renderNewConnections(); }, 1000);
});
"""


def admin_dashboard_url(stack_fqdn: str, https_port: int) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{stack_fqdn}{port}/admin"


def build_ingress_app(
    *,
    admin_url: str,
    status: Callable[[], dict[str, Any]],
    set_new_connections_enabled: Callable[[bool], object],
) -> FastAPI:
    page = _PAGE.replace("{admin_url}", html.escape(admin_url, quote=True))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def only_home_assistant_ingress(request: Request, call_next: Callable[[Request], Any]) -> Response:
        client_host = request.client.host if request.client is not None else ""
        if client_host != HA_INGRESS_PROXY:
            return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/")
    async def panel_page() -> Response:
        return HTMLResponse(page)

    @app.get("/panel.css")
    async def panel_css() -> Response:
        return Response(_CSS, media_type="text/css")

    @app.get("/panel.js")
    async def panel_js() -> Response:
        return Response(_JS, media_type="text/javascript")

    @app.get("/api/status")
    async def panel_status() -> Response:
        return JSONResponse(status())

    @app.post("/api/new-connections")
    async def panel_new_connections(request: Request) -> Response:
        # A JSON content type cannot be sent cross-site without a CORS preflight,
        # which this app never answers.
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "enabled must be a boolean"}, status_code=400)
        set_new_connections_enabled(enabled)
        user = request.headers.get("x-remote-user-display-name") or request.headers.get("x-remote-user-name") or "unknown user"
        LOG.info("New connections %s from Home Assistant by %s", "opened" if enabled else "closed", user)
        return JSONResponse(status())

    return app
