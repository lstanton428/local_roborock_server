"""Request bodies are bounded before routing, and large bodies are traced by hash only."""

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from conftest import write_release_config
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import (
    MAX_REQUEST_BODY_BYTES,
    MAX_TRACED_BODY_BYTES,
    ReleaseSupervisor,
    RequestBodyLimitMiddleware,
)


@pytest.fixture
def supervisor(tmp_path: Path) -> ReleaseSupervisor:
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    return ReleaseSupervisor(config=config, paths=resolve_paths(config_file, config))


def _trace_entries(supervisor: ReleaseSupervisor) -> list[dict]:
    path = supervisor.paths.http_jsonl_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_oversized_content_length_is_rejected_without_reading_body() -> None:
    called: list[str] = []

    async def app(scope, receive, send):
        called.append("app")

    async def receive():
        raise AssertionError("body must not be read")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(app, max_body_bytes=1024)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/nc",
        "headers": [(b"content-length", str(10**9).encode())],
    }
    asyncio.run(middleware(scope, receive, send))

    assert called == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_oversized_content_length_request_gets_413(supervisor: ReleaseSupervisor) -> None:
    client = TestClient(supervisor.app)
    response = client.post("/nc?did=1", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert response.status_code == 413
    assert _trace_entries(supervisor) == []


def test_oversized_chunked_body_gets_413(supervisor: ReleaseSupervisor) -> None:
    def chunks():
        chunk = b"x" * 65536
        for _ in range(MAX_REQUEST_BODY_BYTES // len(chunk) + 2):
            yield chunk

    client = TestClient(supervisor.app)
    request = client.build_request("POST", "/api/v1/upload", content=chunks())
    assert "content-length" not in request.headers
    response = client.send(request)
    assert response.status_code == 413
    assert _trace_entries(supervisor) == []


def test_oversized_admin_body_gets_413(supervisor: ReleaseSupervisor) -> None:
    client = TestClient(supervisor.app)
    response = client.post(
        "/admin/api/login",
        content=b'{"password": "' + b"x" * MAX_REQUEST_BODY_BYTES + b'"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_body_under_limit_is_handled_and_traced_by_hash(supervisor: ReleaseSupervisor) -> None:
    body = b"a=" + b"x" * (MAX_TRACED_BODY_BYTES * 4)
    client = TestClient(supervisor.app)
    response = client.post(
        "/api/v1/unknown-endpoint",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code != 413
    [entry] = _trace_entries(supervisor)
    assert entry["body_len"] == len(body)
    assert entry["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert entry["body_omitted"] is True
    for key in ("body_b64", "body_text", "body_json", "body_form"):
        assert key not in entry


def test_small_body_is_still_traced(supervisor: ReleaseSupervisor) -> None:
    client = TestClient(supervisor.app)
    response = client.post("/api/v1/unknown-endpoint", json={"hello": "world"})
    assert response.status_code != 413
    [entry] = _trace_entries(supervisor)
    assert entry["body_json"] == {"hello": "world"}
    assert "body_b64" in entry and "body_omitted" not in entry
