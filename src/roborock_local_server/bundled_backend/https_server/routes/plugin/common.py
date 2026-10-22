"""Shared plugin constants and services used by plugin routes."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from fastapi.responses import FileResponse, Response

from shared.context import ServerContext
from ..auth.service import load_cloud_full_snapshot

PLUGIN_PROXY_ALLOWED_HOSTS = {
    "files.roborock.com",
    "app-files.roborock.com",
    "rrpkg-us.roborock.com",
    "cdn.awsusor0.fds.api.mi-img.com",
}

# Some built-in plugins are over 50 MiB; downloads stream to disk, not memory.
PLUGIN_PROXY_MAX_BYTES = 128 * 1024 * 1024
PLUGIN_PROXY_CACHE_MAX_BYTES = 512 * 1024 * 1024
PLUGIN_PROXY_READ_CHUNK_BYTES = 64 * 1024
# Plugin zips are published as <32 hex>.zip under /iot/plugin/ (Roborock hosts)
# or /resources/iot/plugin/ (Xiaomi FDS CDN).
PLUGIN_SOURCE_PATH_RE = re.compile(r"(?:/resources)?/iot/plugin/[0-9a-f]{32}\.zip")
PLUGIN_PROXY_MAX_REDIRECTS = 5
# Each download writes up to PLUGIN_PROXY_MAX_BYTES to disk, so cap how many run at once.
PLUGIN_PROXY_MAX_CONCURRENT_DOWNLOADS = 4
PLUGIN_PROXY_DOWNLOAD_SLOT_WAIT_SECONDS = 30.0
# Upstream failures (such as 404 for an unknown name) are remembered per URL so
# repeats fail fast instead of taking a download slot again.
PLUGIN_PROXY_FAILURE_TTL_SECONDS = 600.0
PLUGIN_PROXY_MAX_REMEMBERED_FAILURES = 256
PLUGIN_PROXY_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

LEGACY_CATEGORY_PLUGIN_SOURCES = {
    "robot_vacuum_cleaner": "https://files.roborock.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip",
    "roborock_wetdryvac": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/10320c51139848e9ade1e6bd231e15c8.zip",
    "roborock_wm": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/7f2a3e398aa54427afb48461f69a1a8c.zip",
}

APP_FEATURE_PLUGIN_LIST = [
    {
        "moduleType": "DEVICE_PAIRING",
        "version": 120,
        "apiLevel": 10028,
        "url": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/0e2aad7a7c0b4721ac06c415b48bd0a8.zip",
        "pluginLevel": 3001,
        "scope": None,
    },
    {
        "moduleType": "PERSONAL_CENTER",
        "version": 292,
        "apiLevel": 10044,
        "url": "https://app-files.roborock.com/iot/plugin/ddb433cf4f2b43c9b553aea3ace73f4e.zip",
        "pluginLevel": 3002,
        "scope": None,
    },
]

CATEGORY_PLUGIN_LIST = [
    {
        "categoryId": 1,
        "category": "robot.vacuum.cleaner",
        "md5": None,
        "version": 2050,
        "apiLevel": 10028,
        "url": "https://files.roborock.com/iot/plugin/979bb22f91a24f10a8bafe232b4fb5ee.zip",
        "pluginLevel": 1,
        "scope": None,
    },
    {
        "categoryId": 2,
        "category": "roborock.wetdryvac",
        "md5": None,
        "version": 1024,
        "apiLevel": 10028,
        "url": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/10320c51139848e9ade1e6bd231e15c8.zip",
        "pluginLevel": 1,
        "scope": None,
    },
    {
        "categoryId": 3,
        "category": "roborock.wm",
        "md5": None,
        "version": 1014,
        "apiLevel": 10028,
        "url": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/7f2a3e398aa54427afb48461f69a1a8c.zip",
        "pluginLevel": 1,
        "scope": None,
    },
]

APPPLUGIN_LIST = [
    {
        "version": 6058,
        "url": "https://files.roborock.com/iot/plugin/ea53983b82e948638904d9154bb7f474.zip",
        "pluginLevel": 3001,
        "productid": 110,
        "apilevel": 10028,
    },
    {
        "version": 5208,
        "url": "https://cdn.awsusor0.fds.api.mi-img.com/resources/iot/plugin/7cf4eb4c705c420483741189be389927.zip",
        "pluginLevel": 3001,
        "productid": 23,
        "apilevel": 10028,
    },
    {
        "version": 90,
        "url": "https://rrpkg-us.roborock.com/iot/plugin/019b4e083fbe7f81a28d79756be6f0ed.zip",
        "pluginLevel": 3001,
        "productid": 10001,
        "apilevel": 10028,
    },
]


def plugin_proxy_url(ctx: ServerContext, source_url: str) -> str:
    source = str(source_url or "").strip()
    # The proxy only serves canonical sources; hand anything else to the app
    # unchanged rather than as a proxy link that would always be refused.
    if not source or not is_canonical_plugin_source(source):
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    encoded_source = quote(source, safe="")
    return f"{ctx.api_url()}/plugin/proxy/{digest}.zip?src={encoded_source}"


def proxied_plugin_records(
    ctx: ServerContext,
    records: Sequence[dict[str, Any]],
    *,
    url_key: str = "url",
) -> list[dict[str, Any]]:
    proxied: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        item = dict(record)
        source_url = str(item.get(url_key) or "").strip()
        if source_url:
            item[url_key] = plugin_proxy_url(ctx, source_url)
        proxied.append(item)
    return proxied


def _snapshot_plugin_catalogs(ctx: ServerContext) -> dict[str, Any]:
    snapshot = load_cloud_full_snapshot(ctx) or {}
    web_cache = snapshot.get("web_api_cache")
    if not isinstance(web_cache, dict):
        return {}
    catalogs = web_cache.get("plugin_catalogs")
    return catalogs if isinstance(catalogs, dict) else {}


def _catalog_data(catalogs: dict[str, Any], name: str) -> Any:
    catalog = catalogs.get(name)
    if not isinstance(catalog, dict):
        return None
    return catalog.get("data")


def _dict_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def snapshot_plugin_records(
    ctx: ServerContext,
    catalog_name: str,
    *,
    list_key: str | None = None,
) -> list[dict[str, Any]]:
    data = _catalog_data(_snapshot_plugin_catalogs(ctx), catalog_name)
    if list_key is not None:
        if not isinstance(data, dict):
            return []
        return _dict_records(data.get(list_key))
    return _dict_records(data)


def plugin_records_with_snapshot_fallback(
    ctx: ServerContext,
    catalog_name: str,
    fallback_records: Sequence[dict[str, Any]],
    *,
    list_key: str | None = None,
) -> list[dict[str, Any]]:
    snapshot_records = snapshot_plugin_records(ctx, catalog_name, list_key=list_key)
    return snapshot_records or list(fallback_records)


def first_query_value(query_params: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = query_params.get(key) or []
        for value in values:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
    return ""


def is_allowed_plugin_source(source_url: str) -> bool:
    parsed = urlparse(source_url)
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port not in (None, 443):
        return False
    if host in PLUGIN_PROXY_ALLOWED_HOSTS:
        return True
    return host.endswith(".fds.api.mi-img.com")


def is_canonical_plugin_source(source_url: str) -> bool:
    """Accept only the exact plugin zip URL shapes the cloud publishes.

    The proxy route is unauthenticated and caches by source URL, so any accepted
    variation (query string, port, userinfo, alternate path) would be a fresh
    download and cache entry.
    """
    if not is_allowed_plugin_source(source_url):
        return False
    parsed = urlparse(source_url)
    # Rebuilding from host + path drops any port, userinfo, params, query or fragment.
    if source_url != f"https://{parsed.hostname}{parsed.path}":
        return False
    return PLUGIN_SOURCE_PATH_RE.fullmatch(parsed.path) is not None


def plugin_cache_path(runtime_dir: Path, source_url: str) -> Path:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    return runtime_dir / "plugin_proxy_cache" / f"{digest}.zip"


class PluginUpstreamError(RuntimeError):
    """The upstream response itself was unusable (bad status, redirect, size or body)."""


async def download_plugin_zip(source_url: str, dest_path: Path) -> str:
    """Stream the plugin zip at source_url into dest_path and return its content type.

    The caller owns dest_path and removes it if this raises.
    """
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "roborock-local-server/0.1"}
    url = source_url
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # Follow redirects by hand so every hop must stay on an allowed plugin host.
        for _hop in range(PLUGIN_PROXY_MAX_REDIRECTS + 1):
            async with session.get(url, allow_redirects=False) as response:
                status = int(response.status)
                if status in PLUGIN_PROXY_REDIRECT_STATUSES:
                    location = str(response.headers.get("Location") or "").strip()
                    if not location:
                        raise PluginUpstreamError(f"upstream returned HTTP {status} without a Location")
                    next_url = urljoin(url, location)
                    if not is_allowed_plugin_source(next_url):
                        raise PluginUpstreamError(
                            f"upstream redirected to a disallowed host: {urlparse(next_url).hostname}"
                        )
                    url = next_url
                    continue
                if status != 200:
                    raise PluginUpstreamError(f"upstream returned HTTP {status}")
                content_length = str(response.headers.get("Content-Length") or "").strip()
                if content_length.isdigit() and int(content_length) > PLUGIN_PROXY_MAX_BYTES:
                    raise PluginUpstreamError(
                        f"plugin too large: {content_length} bytes exceeds {PLUGIN_PROXY_MAX_BYTES} byte limit"
                    )
                size = 0
                with dest_path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(PLUGIN_PROXY_READ_CHUNK_BYTES):
                        size += len(chunk)
                        if size > PLUGIN_PROXY_MAX_BYTES:
                            raise PluginUpstreamError(f"plugin too large: exceeds {PLUGIN_PROXY_MAX_BYTES} byte limit")
                        handle.write(chunk)
                if not size:
                    raise PluginUpstreamError("upstream returned empty content")
                return str(response.headers.get("Content-Type") or "application/zip").strip()
    raise PluginUpstreamError(f"upstream redirected more than {PLUGIN_PROXY_MAX_REDIRECTS} times")


def evict_plugin_cache(cache_dir: Path, *, keep: Path, max_bytes: int) -> None:
    """Delete least-recently-used cached zips until the cache fits in max_bytes."""
    entries: list[tuple[float, int, Path]] = []
    for path in cache_dir.glob("*.zip"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _mtime, size, _path in entries)
    for _mtime, size, path in sorted(entries, key=lambda entry: entry[0]):
        if total <= max_bytes:
            break
        if path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


class _PluginDownloads:
    """Bound the plugin downloads the unauthenticated proxy route can start.

    Requests for a URL that is already downloading share that download, and at
    most PLUGIN_PROXY_MAX_CONCURRENT_DOWNLOADS different URLs download at once.
    Upstream failures are remembered for PLUGIN_PROXY_FAILURE_TTL_SECONDS so a
    repeated request for a missing plugin fails without downloading again.
    asyncio primitives belong to one event loop, so the state is rebuilt when
    the running loop changes.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        self._in_flight: dict[str, asyncio.Future[str]] = {}
        self._failures: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def fetch(self, source_url: str, cache_path: Path) -> str:
        loop = asyncio.get_running_loop()
        if loop is not self._loop or self._slots is None:
            self._loop = loop
            self._slots = asyncio.Semaphore(PLUGIN_PROXY_MAX_CONCURRENT_DOWNLOADS)
            self._in_flight = {}
        self._raise_remembered_failure(source_url)
        task = self._in_flight.get(source_url)
        if task is None:
            task = asyncio.ensure_future(self._download_to_cache(self._slots, source_url, cache_path))
            self._in_flight[source_url] = task
            task.add_done_callback(lambda done: self._finished(source_url, done))
        # Shielded so a client that disconnects does not cancel the download for
        # the other requests waiting on it.
        return await asyncio.shield(task)

    def _raise_remembered_failure(self, source_url: str) -> None:
        remembered = self._failures.get(source_url)
        if remembered is None:
            return
        expires_at, message = remembered
        if time.monotonic() >= expires_at:
            del self._failures[source_url]
            return
        raise PluginUpstreamError(f"{message} (recent failure; not retried yet)")

    def _remember_failure(self, source_url: str, exc: BaseException) -> None:
        self._failures.pop(source_url, None)
        self._failures[source_url] = (time.monotonic() + PLUGIN_PROXY_FAILURE_TTL_SECONDS, str(exc))
        while len(self._failures) > PLUGIN_PROXY_MAX_REMEMBERED_FAILURES:
            self._failures.popitem(last=False)

    def _finished(self, source_url: str, done: asyncio.Future[str]) -> None:
        if self._in_flight.get(source_url) is done:
            del self._in_flight[source_url]
        if done.cancelled():
            return
        exc = done.exception()  # Mark retrieved even if every waiter went away.
        if isinstance(exc, PluginUpstreamError):
            self._remember_failure(source_url, exc)

    @staticmethod
    async def _download_to_cache(
        slots: asyncio.Semaphore,
        source_url: str,
        cache_path: Path,
    ) -> str:
        try:
            await asyncio.wait_for(slots.acquire(), timeout=PLUGIN_PROXY_DOWNLOAD_SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("too many plugin downloads in progress") from exc
        temp_path = cache_path.with_suffix(".tmp")
        try:
            content_type = await download_plugin_zip(source_url, temp_path)
            temp_path.replace(cache_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        finally:
            slots.release()
        evict_plugin_cache(cache_path.parent, keep=cache_path, max_bytes=PLUGIN_PROXY_CACHE_MAX_BYTES)
        return content_type


_plugin_downloads = _PluginDownloads()


def _cached_zip_response(cache_path: Path, *, media_type: str, cache_state: str) -> FileResponse:
    return FileResponse(
        cache_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400", "X-RR-Plugin-Cache": cache_state},
    )


async def plugin_proxy_response(*, runtime_dir: Path, source_url: str) -> Response:
    cache_path = plugin_cache_path(runtime_dir, source_url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        # Refresh mtime so eviction treats it as recently used.
        os.utime(cache_path)
        return _cached_zip_response(cache_path, media_type="application/zip", cache_state="hit")

    upstream_content_type = await _plugin_downloads.fetch(source_url, cache_path)
    media_type = upstream_content_type if "zip" in upstream_content_type.lower() else "application/zip"
    return _cached_zip_response(cache_path, media_type=media_type, cache_state="miss")
