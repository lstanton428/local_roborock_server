from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ServerContext


def secret_equals(provided: str, expected: str) -> bool:
    """Constant-time string comparison for tokens and passwords."""
    # compare_digest rejects non-ASCII str, so compare the encoded bytes instead.
    return hmac.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def get_value(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        return value
    return default


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def stable_int(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16)


def default_home_id(ctx: ServerContext) -> int:
    return stable_int(f"{ctx.duid}:home")


def default_product_name(model: str) -> str:
    short_model = model.split(".")[-1].upper() if model else "VACUUM"
    return f"Roborock {short_model}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
