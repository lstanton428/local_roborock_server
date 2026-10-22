"""Route helpers for /plugin/category/*.zip."""

from __future__ import annotations

from .common import LEGACY_CATEGORY_PLUGIN_SOURCES, is_canonical_plugin_source


def match_category_zip_path(path: str) -> bool:
    clean = path.rstrip("/")
    return clean.startswith("/plugin/category/") and clean.endswith(".zip")


def source_from_category_path(clean_path: str) -> str:
    if not match_category_zip_path(clean_path):
        return ""
    slug = clean_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".zip").strip().lower()
    source = LEGACY_CATEGORY_PLUGIN_SOURCES.get(slug, "")
    if source and is_canonical_plugin_source(source):
        return source
    return ""

