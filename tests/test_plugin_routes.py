import asyncio
import json
import os
import urllib.parse
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from conftest import write_release_config
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import ReleaseSupervisor, resolve_route


def _build_supervisor(tmp_path: Path) -> ReleaseSupervisor:
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    return ReleaseSupervisor(config=config, paths=paths)


def _source_from_proxied_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.parse_qs(parsed.query).get("src", [""])[0]


def test_api_v1_plugins_returns_proxied_category_urls(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)

    response = client.get("/api/v1/plugins")
    assert response.status_code == 200

    payload = response.json()
    assert payload["code"] == 200
    records = payload["data"]["categoryPluginList"]
    assert isinstance(records, list)
    assert records

    for item in records:
        url = str(item["url"])
        assert url.startswith("https://api-roborock.example.com/plugin/proxy/")
        source = _source_from_proxied_url(url)
        assert source.startswith("https://")


def test_plugin_endpoints_prefer_cloud_snapshot_catalogs(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path)
    snapshot = {
        "web_api_cache": {
            "plugin_catalogs": {
                "plugins": {
                    "data": {
                        "categoryPluginList": [
                            {
                                "categoryId": 99,
                                "category": "robot.vacuum.cleaner",
                                "version": 9999,
                                "apiLevel": 10043,
                                "url": "https://files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd01.zip",
                                "pluginLevel": 1,
                            }
                        ]
                    }
                },
                "appfeatureplugin": {
                    "data": {
                        "plugins": [
                            {
                                "moduleType": "PERSONAL_CENTER",
                                "version": 999,
                                "apiLevel": 10043,
                                "url": "https://app-files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd02.zip",
                                "pluginLevel": 3002,
                                "scope": "cloud",
                            }
                        ]
                    }
                },
                "appplugin": {
                    "data": [
                        {
                            "version": 8888,
                            "url": "https://rrpkg-us.roborock.com/iot/plugin/0123456789abcdef0123456789abcd03.zip",
                            "pluginLevel": 3001,
                            "productid": 110,
                            "apilevel": 10043,
                        }
                    ]
                },
            }
        }
    }
    supervisor.paths.cloud_snapshot_path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
    client = TestClient(supervisor.app)

    category_payload = client.get("/api/v1/plugins").json()
    category_record = category_payload["data"]["categoryPluginList"][0]
    assert category_record["version"] == 9999
    assert _source_from_proxied_url(category_record["url"]) == "https://files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd01.zip"

    feature_payload = client.get("/api/v1/appfeatureplugin").json()
    feature_record = feature_payload["data"]["plugins"][0]
    assert feature_record["version"] == 999
    assert _source_from_proxied_url(feature_record["url"]) == "https://app-files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd02.zip"

    app_plugin_payload = client.get("/api/v1/appplugin").json()
    app_plugin_record = app_plugin_payload["data"][0]
    assert app_plugin_record["version"] == 8888
    assert _source_from_proxied_url(app_plugin_record["url"]) == "https://rrpkg-us.roborock.com/iot/plugin/0123456789abcdef0123456789abcd03.zip"


def test_app_plugin_endpoints_return_proxied_urls(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)

    app_plugin_response = client.get("/api/v1/appplugin")
    assert app_plugin_response.status_code == 200
    app_plugin_payload = app_plugin_response.json()
    assert app_plugin_payload["code"] == 200
    for item in app_plugin_payload["data"]:
        assert str(item["url"]).startswith("https://api-roborock.example.com/plugin/proxy/")

    feature_response = client.get("/api/v1/appfeatureplugin")
    assert feature_response.status_code == 200
    feature_payload = feature_response.json()
    assert feature_payload["code"] == 200
    for item in feature_payload["data"]["plugins"]:
        assert str(item["url"]).startswith("https://api-roborock.example.com/plugin/proxy/")


def test_plugin_category_zip_uses_expected_source_and_cache(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)
    downloaded_sources: list[str] = []

    async def fake_download(source_url: str) -> tuple[bytes, str]:
        downloaded_sources.append(source_url)
        return b"fake-zip-bytes", "application/zip"

    monkeypatch.setattr(plugin_common, "download_plugin_zip", fake_download)

    expected_source = plugin_common.LEGACY_CATEGORY_PLUGIN_SOURCES["robot_vacuum_cleaner"]
    first = client.get("/plugin/category/robot_vacuum_cleaner.zip")
    assert first.status_code == 200
    assert first.content == b"fake-zip-bytes"
    assert first.headers["X-RR-Plugin-Cache"] == "miss"
    assert downloaded_sources == [expected_source]

    second = client.get("/plugin/category/robot_vacuum_cleaner.zip")
    assert second.status_code == 200
    assert second.content == b"fake-zip-bytes"
    assert second.headers["X-RR-Plugin-Cache"] == "hit"
    assert downloaded_sources == [expected_source]


def test_plugin_proxy_rejects_non_https_and_unknown_hosts(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)

    async def fail_download(source_url: str) -> tuple[bytes, str]:
        raise AssertionError(f"download should not be called for {source_url}")

    monkeypatch.setattr(plugin_common, "download_plugin_zip", fail_download)

    blocked_sources = (
        "http://files.roborock.com/iot/plugin/blocked.zip",
        "https://example.com/iot/plugin/blocked.zip",
    )
    for source in blocked_sources:
        encoded = urllib.parse.quote(source, safe="")
        response = client.get(f"/plugin/proxy/blocked.zip?src={encoded}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["route"] == "/plugin/proxy/blocked.zip"


def test_non_plugin_routes_still_resolve_to_same_handlers(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path)
    route_name, payload = resolve_route(
        rules=supervisor.endpoint_rules,
        context=supervisor.context,
        clean_path="/api/v1/userInfo",
        query_params={},
        body_params={},
        method="GET",
    )
    assert route_name == "user_info"
    assert payload["code"] == 200


class _FakePluginContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_read = 0

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


class _FakePluginResponse:
    def __init__(
        self,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        *,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakePluginContent(chunks if chunks is not None else [body])

    async def __aenter__(self) -> "_FakePluginResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakePluginSession:
    def __init__(self, responses: dict[str, _FakePluginResponse], requested: list[str]) -> None:
        self._responses = responses
        self._requested = requested

    async def __aenter__(self) -> "_FakePluginSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def get(self, url: str, *, allow_redirects: bool) -> _FakePluginResponse:
        assert allow_redirects is False
        self._requested.append(url)
        return self._responses[url]


def _patch_plugin_session(monkeypatch, responses: dict[str, _FakePluginResponse]) -> list[str]:
    from https_server.routes.plugin import common as plugin_common

    requested: list[str] = []
    monkeypatch.setattr(
        plugin_common.aiohttp,
        "ClientSession",
        lambda **_kwargs: _FakePluginSession(responses, requested),
    )
    return requested


def test_plugin_download_follows_redirects_between_allowed_hosts(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    requested = _patch_plugin_session(
        monkeypatch,
        {
            "https://files.roborock.com/iot/plugin/a.zip": _FakePluginResponse(
                302, {"Location": "https://app-files.roborock.com/iot/plugin/a.zip"}
            ),
            "https://app-files.roborock.com/iot/plugin/a.zip": _FakePluginResponse(
                301, {"Location": "/iot/plugin/b.zip"}
            ),
            "https://app-files.roborock.com/iot/plugin/b.zip": _FakePluginResponse(
                200, {"Content-Type": "application/zip"}, b"zip-bytes"
            ),
        },
    )

    data, content_type = asyncio.run(plugin_common.download_plugin_zip("https://files.roborock.com/iot/plugin/a.zip"))

    assert data == b"zip-bytes"
    assert content_type == "application/zip"
    assert requested[-1] == "https://app-files.roborock.com/iot/plugin/b.zip"


def test_plugin_download_refuses_redirect_to_disallowed_host(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    requested = _patch_plugin_session(
        monkeypatch,
        {
            "https://files.roborock.com/iot/plugin/a.zip": _FakePluginResponse(
                302, {"Location": "http://169.254.169.254/latest/meta-data/"}
            ),
        },
    )

    with pytest.raises(RuntimeError, match="disallowed host"):
        asyncio.run(plugin_common.download_plugin_zip("https://files.roborock.com/iot/plugin/a.zip"))
    assert requested == ["https://files.roborock.com/iot/plugin/a.zip"]


def test_plugin_download_limits_redirect_chains(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    loop_url = "https://files.roborock.com/iot/plugin/loop.zip"
    requested = _patch_plugin_session(monkeypatch, {loop_url: _FakePluginResponse(302, {"Location": loop_url})})

    with pytest.raises(RuntimeError, match="redirected more than"):
        asyncio.run(plugin_common.download_plugin_zip(loop_url))
    assert len(requested) == plugin_common.PLUGIN_PROXY_MAX_REDIRECTS + 1


def test_plugin_download_checks_every_redirect_hop(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    disallowed = "https://evil.example.com/iot/plugin/c.zip"
    requested = _patch_plugin_session(
        monkeypatch,
        {
            "https://files.roborock.com/iot/plugin/a.zip": _FakePluginResponse(
                302, {"Location": "https://app-files.roborock.com/iot/plugin/b.zip"}
            ),
            "https://app-files.roborock.com/iot/plugin/b.zip": _FakePluginResponse(302, {"Location": disallowed}),
            disallowed: _FakePluginResponse(200, {"Content-Type": "application/zip"}, b"evil"),
        },
    )

    with pytest.raises(RuntimeError, match="disallowed host"):
        asyncio.run(plugin_common.download_plugin_zip("https://files.roborock.com/iot/plugin/a.zip"))
    assert requested == [
        "https://files.roborock.com/iot/plugin/a.zip",
        "https://app-files.roborock.com/iot/plugin/b.zip",
    ]


def test_plugin_download_refuses_redirect_to_explicit_port(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    requested = _patch_plugin_session(
        monkeypatch,
        {
            "https://files.roborock.com/iot/plugin/a.zip": _FakePluginResponse(
                302, {"Location": "https://app-files.roborock.com:8443/iot/plugin/b.zip"}
            ),
        },
    )

    with pytest.raises(RuntimeError, match="disallowed host"):
        asyncio.run(plugin_common.download_plugin_zip("https://files.roborock.com/iot/plugin/a.zip"))
    assert requested == ["https://files.roborock.com/iot/plugin/a.zip"]


def test_allowed_plugin_source_requires_default_https_port() -> None:
    from https_server.routes.plugin import common as plugin_common

    assert plugin_common.is_allowed_plugin_source("https://files.roborock.com/iot/plugin/a.zip")
    assert plugin_common.is_allowed_plugin_source("https://files.roborock.com:443/iot/plugin/a.zip")
    assert not plugin_common.is_allowed_plugin_source("https://files.roborock.com:8443/iot/plugin/a.zip")
    assert not plugin_common.is_allowed_plugin_source("https://files.roborock.com:80/iot/plugin/a.zip")
    assert not plugin_common.is_allowed_plugin_source("https://files.roborock.com:notaport/iot/plugin/a.zip")


def test_plugin_download_rejects_oversized_content_length_without_reading(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    source = "https://files.roborock.com/iot/plugin/a.zip"
    response = _FakePluginResponse(
        200,
        {"Content-Length": str(plugin_common.PLUGIN_PROXY_MAX_BYTES + 1)},
        b"small-body",
    )
    _patch_plugin_session(monkeypatch, {source: response})

    with pytest.raises(RuntimeError, match="too large"):
        asyncio.run(plugin_common.download_plugin_zip(source))
    assert response.content.chunks_read == 0


def test_plugin_download_stops_reading_streamed_oversize_body(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    monkeypatch.setattr(plugin_common, "PLUGIN_PROXY_MAX_BYTES", 10)
    source = "https://files.roborock.com/iot/plugin/a.zip"
    response = _FakePluginResponse(200, chunks=[b"123456"] * 100)
    _patch_plugin_session(monkeypatch, {source: response})

    with pytest.raises(RuntimeError, match="too large"):
        asyncio.run(plugin_common.download_plugin_zip(source))
    assert response.content.chunks_read == 2


def test_plugin_download_accepts_body_at_size_limit(monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    monkeypatch.setattr(plugin_common, "PLUGIN_PROXY_MAX_BYTES", 12)
    source = "https://files.roborock.com/iot/plugin/a.zip"
    _patch_plugin_session(
        monkeypatch,
        {source: _FakePluginResponse(200, {"Content-Length": "12"}, chunks=[b"123456", b"789012"])},
    )

    data, _content_type = asyncio.run(plugin_common.download_plugin_zip(source))
    assert data == b"123456789012"


def _known_plugin_sources() -> list[str]:
    from https_server.routes.plugin import common as plugin_common

    sources = list(plugin_common.LEGACY_CATEGORY_PLUGIN_SOURCES.values())
    for records in (
        plugin_common.APP_FEATURE_PLUGIN_LIST,
        plugin_common.CATEGORY_PLUGIN_LIST,
        plugin_common.APPPLUGIN_LIST,
    ):
        sources.extend(str(record["url"]) for record in records)
    return sources


def test_plugin_proxy_accepts_every_known_plugin_source(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)
    downloaded_sources: list[str] = []

    async def fake_download(source_url: str) -> tuple[bytes, str]:
        downloaded_sources.append(source_url)
        return b"fake-zip-bytes", "application/zip"

    monkeypatch.setattr(plugin_common, "download_plugin_zip", fake_download)

    known_sources = _known_plugin_sources()
    assert len(known_sources) == 11
    for source in known_sources:
        assert plugin_common.is_canonical_plugin_source(source), source
        proxied = plugin_common.plugin_proxy_url(supervisor.context, source)
        response = client.get(proxied.removeprefix("https://api-roborock.example.com"))
        assert response.status_code == 200, source
        assert response.content == b"fake-zip-bytes", source
    assert set(downloaded_sources) == set(known_sources)


def test_plugin_proxy_rejects_non_canonical_sources(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    supervisor = _build_supervisor(tmp_path)
    client = TestClient(supervisor.app)

    async def fail_download(source_url: str) -> tuple[bytes, str]:
        raise AssertionError(f"download should not be called for {source_url}")

    monkeypatch.setattr(plugin_common, "download_plugin_zip", fail_download)

    roborock = "https://files.roborock.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip"
    mi_img = "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/10320c51139848e9ade1e6bd231e15c8.zip"
    blocked_sources = (
        # query string / fragment variants
        f"{roborock}?v=1",
        f"{roborock}?",
        f"{roborock}#frag",
        f"{mi_img}?cache=bust",
        # explicit ports, including the default one
        roborock.replace("files.roborock.com", "files.roborock.com:443"),
        roborock.replace("files.roborock.com", "files.roborock.com:8443"),
        # userinfo and non-lowercase host/scheme
        roborock.replace("files.roborock.com", "user@files.roborock.com"),
        roborock.replace("files.roborock.com", "FILES.roborock.com"),
        roborock.replace("https://", "HTTPS://"),
        # non-canonical paths
        roborock.replace("/iot/plugin/", "/iot/plugin/x/"),
        roborock.replace("/iot/plugin/", "/iot//plugin/"),
        roborock.replace("/iot/plugin/", "/iot/plugin/../plugin/"),
        roborock.replace(".zip", ".ZIP"),
        roborock.replace(".zip", ".zip;x"),
        roborock.replace("979bb22f", "979BB22F"),
        roborock.replace("979bb22f", "979bb22"),
        roborock.replace("979bb22f", "979bb22f0"),
        "https://files.roborock.com/iot/plugin/blocked.zip",
        "https://files.roborock.com/other/979bb22f91a24f10a8bafe232b4fb5ee.zip",
        roborock.replace("/plugin/", "/plu\tgin/"),
    )
    for source in blocked_sources:
        assert not plugin_common.is_canonical_plugin_source(source), source
        encoded = urllib.parse.quote(source, safe="")
        response = client.get(f"/plugin/proxy/blocked.zip?src={encoded}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"]["route"] == "/plugin/proxy/blocked.zip", source


def test_plugin_cache_evicts_least_recently_used_files(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    monkeypatch.setattr(plugin_common, "PLUGIN_PROXY_CACHE_MAX_BYTES", 25)
    cache_dir = tmp_path / "plugin_proxy_cache"
    cache_dir.mkdir()
    oldest = cache_dir / "oldest.zip"
    older = cache_dir / "older.zip"
    oldest.write_bytes(b"a" * 10)
    older.write_bytes(b"b" * 10)
    os.utime(oldest, (1_000, 1_000))
    os.utime(older, (2_000, 2_000))

    hit_source = "https://files.roborock.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip"
    hit_path = plugin_common.plugin_cache_path(tmp_path, hit_source)
    hit_path.write_bytes(b"c" * 5)
    os.utime(hit_path, (500, 500))

    async def fake_download(source_url: str) -> tuple[bytes, str]:
        return b"d" * 10, "application/zip"

    monkeypatch.setattr(plugin_common, "download_plugin_zip", fake_download)

    # A cache hit marks the oldest-looking entry as recently used.
    hit = asyncio.run(plugin_common.plugin_proxy_response(runtime_dir=tmp_path, source_url=hit_source))
    assert hit.headers["X-RR-Plugin-Cache"] == "hit"

    new_source = "https://app-files.roborock.com/iot/plugin/ddb433cf4f2b43c9b553aea3ace73f4e.zip"
    miss = asyncio.run(plugin_common.plugin_proxy_response(runtime_dir=tmp_path, source_url=new_source))
    assert miss.headers["X-RR-Plugin-Cache"] == "miss"

    new_path = plugin_common.plugin_cache_path(tmp_path, new_source)
    remaining = {path.name for path in cache_dir.glob("*.zip")}
    assert remaining == {older.name, hit_path.name, new_path.name}
    assert sum(path.stat().st_size for path in cache_dir.glob("*.zip")) <= 25


def test_plugin_proxy_url_leaves_non_canonical_sources_unproxied(tmp_path: Path) -> None:
    from https_server.routes.plugin import common as plugin_common

    supervisor = _build_supervisor(tmp_path)
    canonical = "https://files.roborock.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip"
    for source in (
        f"{canonical}?version=2",
        "https://files.roborock.com/iot/plugin/latest.zip",
        "https://example.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip",
    ):
        assert plugin_common.plugin_proxy_url(supervisor.context, source) == source
    assert _source_from_proxied_url(plugin_common.plugin_proxy_url(supervisor.context, canonical)) == canonical


_CANONICAL_A = "https://files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd0a.zip"
_CANONICAL_B = "https://files.roborock.com/iot/plugin/0123456789abcdef0123456789abcd0b.zip"


def test_plugin_proxy_shares_one_download_between_concurrent_requests(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    calls: list[str] = []

    async def slow_download(source_url: str) -> tuple[bytes, str]:
        calls.append(source_url)
        await asyncio.sleep(0.05)
        return b"zip-bytes", "application/zip"

    monkeypatch.setattr(plugin_common, "download_plugin_zip", slow_download)

    async def run() -> list[bytes]:
        responses = await asyncio.gather(
            *(plugin_common.plugin_proxy_response(runtime_dir=tmp_path, source_url=_CANONICAL_A) for _ in range(5))
        )
        return [response.body for response in responses]

    assert asyncio.run(run()) == [b"zip-bytes"] * 5
    assert calls == [_CANONICAL_A]


def test_plugin_proxy_limits_concurrent_downloads(tmp_path: Path, monkeypatch) -> None:
    from https_server.routes.plugin import common as plugin_common

    monkeypatch.setattr(plugin_common, "PLUGIN_PROXY_MAX_CONCURRENT_DOWNLOADS", 1)
    monkeypatch.setattr(plugin_common, "PLUGIN_PROXY_DOWNLOAD_SLOT_WAIT_SECONDS", 0.05)
    release = None
    calls: list[str] = []

    async def blocking_download(source_url: str) -> tuple[bytes, str]:
        calls.append(source_url)
        await release.wait()
        return b"zip-bytes", "application/zip"

    monkeypatch.setattr(plugin_common, "download_plugin_zip", blocking_download)

    async def run() -> None:
        nonlocal release
        release = asyncio.Event()
        first = asyncio.ensure_future(plugin_common.plugin_proxy_response(runtime_dir=tmp_path, source_url=_CANONICAL_A))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="too many plugin downloads"):
            await plugin_common.plugin_proxy_response(runtime_dir=tmp_path, source_url=_CANONICAL_B)
        release.set()
        assert (await first).body == b"zip-bytes"

    asyncio.run(run())
    assert calls == [_CANONICAL_A]
