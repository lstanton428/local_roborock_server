"""I/O and logging helpers."""

from __future__ import annotations

from collections.abc import Iterable
import errno
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from roborock_local_server.config import LoggingConfig
from roborock_local_server.log_retention import (
    BoundedRotatingFileHandler,
    append_jsonl_text,
)

_FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_STREAM_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_JSONL_STREAM_LOGGER_NAME = "real_stack.jsonl"
_LOGGER = logging.getLogger(__name__)


def _make_stream_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_STREAM_LOG_FORMAT))
    return handler


def _jsonl_stream_logger() -> logging.Logger:
    logger = logging.getLogger(_JSONL_STREAM_LOGGER_NAME)
    logger.propagate = False
    if not logger.handlers:
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        logger.addHandler(_make_stream_handler())
    return logger


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    encoded = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
    append_jsonl_text(path, encoded)
    _jsonl_stream_logger().debug("[%s] %s", path.name, encoded)


def setup_file_logger(
    name: str,
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> logging.Logger:
    defaults = LoggingConfig()
    effective_max_bytes = defaults.max_bytes if max_bytes is None else max_bytes
    effective_backup_count = defaults.backup_count if backup_count is None else backup_count
    logger = logging.getLogger(f"real_stack.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for previous in logger.handlers[:]:
        logger.removeHandler(previous)
        previous.close()
    handler = BoundedRotatingFileHandler(
        path,
        max_bytes=effective_max_bytes,
        backup_count=effective_backup_count,
    )
    handler.setFormatter(logging.Formatter(_FILE_LOG_FORMAT))
    logger.addHandler(handler)
    logger.addHandler(_make_stream_handler())
    return logger


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` as an owner-only (0600) file.

    The text goes to a temporary file in the same directory (mkstemp creates it
    0600 regardless of umask), is fsynced, then renamed over the target. Readers
    see the old or the new contents, and a failed write leaves the old file in
    place. Failures propagate to the caller.
    """
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except OSError as exc:
            # A single-file bind mount cannot be renamed over; keep it writable.
            if exc.errno != errno.EBUSY:
                raise
            path.write_text(text, encoding="utf-8")
            restrict_to_owner([path])
    finally:
        Path(temporary).unlink(missing_ok=True)


def restrict_to_owner(paths: Iterable[Path]) -> list[Path]:
    """Best effort: drop group/other permission bits from existing regular files.

    Returns the files that were changed. Missing files are skipped, and files
    that cannot be changed (for example on a read-only mount) are logged.
    """
    if os.name == "nt":
        return []
    changed: list[Path] = []
    for path in paths:
        try:
            current = path.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(current.st_mode)
        if not stat.S_ISREG(current.st_mode) or not mode & 0o077:
            continue
        try:
            path.chmod(mode & 0o700)
        except OSError as exc:
            _LOGGER.warning(
                "Could not restrict %s to owner-only permissions (%s); run chmod 600 on it",
                path,
                exc.strerror or exc,
            )
            continue
        changed.append(path)
    return changed


def payload_preview(payload: bytes, max_chars: int = 280) -> str:
    if not payload:
        return ""
    try:
        text = payload.decode("utf-8")
        if len(text) > max_chars:
            return text[:max_chars] + "...[truncated]"
        return text
    except UnicodeDecodeError:
        hex_data = payload.hex()
        if len(hex_data) > max_chars:
            return hex_data[:max_chars] + "...[truncated]"
        return hex_data
