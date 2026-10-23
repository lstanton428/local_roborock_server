"""Home Assistant ingress page that sends the browser to the admin dashboard.

Home Assistant builds an add-on's webui link from the hostname the browser used
to reach Home Assistant, which is never the stack hostname the certificate
covers. Ingress opens this page instead, and the page moves the whole browser
tab to the admin dashboard on the stack hostname.
"""

from __future__ import annotations

import base64
import hashlib
import html

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

# Home Assistant's ingress proxy always connects from this address.
HA_INGRESS_PROXY = "172.30.32.2"

# In a browser the top window navigates, not the ingress frame, so the dashboard
# opens as a page of its own instead of framed inside Home Assistant, where its
# login cookie is not sent when the two hostnames are on different sites.
# The Home Assistant apps keep top-window navigation inside their own web view;
# they open only new-window requests in the default browser, and WebKit may
# refuse those without a click, so the link stays on the page to click.
_REDIRECT_SCRIPT = (
    'var url = document.getElementById("admin").href, app = false;'
    " try { app = !!((window.top.webkit && window.top.webkit.messageHandlers"
    " && window.top.webkit.messageHandlers.externalBus) || window.top.externalApp); } catch (e) {}"
    ' if (app) { window.open(url, "_blank"); } else { window.top.location.href = url; }'
)
_REDIRECT_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_REDIRECT_SCRIPT.encode("utf-8")).digest()).decode("ascii")
_PAGE_HEADERS = {
    "Content-Security-Policy": f"default-src 'none'; script-src 'sha256-{_REDIRECT_SCRIPT_HASH}'; base-uri 'none'; form-action 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def admin_dashboard_url(stack_fqdn: str, https_port: int) -> str:
    port = "" if https_port == 443 else f":{https_port}"
    return f"https://{stack_fqdn}{port}/admin"


def build_ingress_app(*, admin_url: str) -> FastAPI:
    escaped_url = html.escape(admin_url, quote=True)
    page = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>Roborock Local Server</title></head>\n'
        f'<body><p><a id="admin" href="{escaped_url}" target="_blank" rel="noopener noreferrer">'
        "Open the Roborock Local Server admin dashboard</a></p>\n"
        f"<script>{_REDIRECT_SCRIPT}</script></body></html>\n"
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def ingress_page(request: Request) -> Response:
        client_host = request.client.host if request.client is not None else ""
        if client_host != HA_INGRESS_PROXY:
            return PlainTextResponse("Forbidden", status_code=403)
        return HTMLResponse(page, headers=_PAGE_HEADERS)

    return app
