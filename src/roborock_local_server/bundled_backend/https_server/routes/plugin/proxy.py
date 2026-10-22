"""Route helpers for /plugin/proxy/*.zip."""

from __future__ import annotations

from .common import first_query_value, is_canonical_plugin_source


def match_proxy_zip_path(path: str) -> bool:
    clean = path.rstrip("/")
    return clean.startswith("/plugin/proxy/") and clean.endswith(".zip")


def source_from_proxy_request(clean_path: str, query_params: dict[str, list[str]]) -> str:
    if not match_proxy_zip_path(clean_path):
        return ""
    source = first_query_value(query_params, "src", "url")
    if source and is_canonical_plugin_source(source):
        return source
    return ""

