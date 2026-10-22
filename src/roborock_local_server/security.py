"""Admin password and session helpers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import secrets
import time


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"pbkdf2_sha256${iterations}$"
        f"{_urlsafe_b64encode(salt)}$"
        f"{_urlsafe_b64encode(digest)}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = stored_hash.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    try:
        iterations = int(raw_iterations)
        salt = _urlsafe_b64decode(raw_salt)
        expected = _urlsafe_b64decode(raw_digest)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class SessionClaims:
    issued_at: int
    expires_at: int


class AdminSessionManager:
    """Stateless HMAC-signed admin session tokens."""

    def __init__(self, *, secret: str, ttl_seconds: int, cookie_name: str = "rrls_admin") -> None:
        self._secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds
        self.cookie_name = cookie_name

    def issue(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now,
            "exp": now + self.ttl_seconds,
            "nonce": secrets.token_urlsafe(16),
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self._secret, raw_payload, hashlib.sha256).digest()
        return f"{_urlsafe_b64encode(raw_payload)}.{_urlsafe_b64encode(signature)}"

    def verify(self, token: str | None) -> SessionClaims | None:
        if not token or "." not in token:
            return None
        raw_payload, raw_sig = token.split(".", 1)
        try:
            payload_bytes = _urlsafe_b64decode(raw_payload)
            signature = _urlsafe_b64decode(raw_sig)
        except Exception:
            return None
        expected = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
        now = int(time.time())
        if expires_at <= now or issued_at > now:
            return None
        return SessionClaims(issued_at=issued_at, expires_at=expires_at)


class LoginThrottle:
    """Back off repeated failed logins per client and, optionally, across all clients.

    Each client gets a few free failures, then must wait an exponentially
    growing delay before the next attempt. A client's failures are forgotten
    once its last failure is older than forget_after_seconds. The optional
    global failure budget bounds guessing spread across many addresses; the
    6-digit protocol PIN only has a million values, so per-address limits alone
    are not enough there. None disables it, so a high-entropy secret such as
    the admin password cannot be locked out for everyone by one noisy client.
    """

    # 2 ** 20 seconds is far beyond any sane max_delay; capping the exponent
    # keeps a huge failure count from overflowing the float.
    _MAX_BACKOFF_EXPONENT = 20

    def __init__(
        self,
        *,
        free_failures: int = 5,
        max_delay_seconds: float = 900.0,
        global_failures_per_window: int | None = 60,
        global_window_seconds: float = 3600.0,
        forget_after_seconds: float = 86400.0,
        max_tracked_clients: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._free_failures = free_failures
        self._max_delay = max_delay_seconds
        self._global_limit = global_failures_per_window
        self._global_window = global_window_seconds
        self._forget_after = forget_after_seconds
        self._max_tracked = max_tracked_clients
        self._clock = clock
        self._clients: dict[str, tuple[int, float]] = {}
        self._global_failures: deque[float] = deque()

    def retry_after(self, client: str) -> float:
        """Seconds the client must wait before trying again, or 0 if allowed."""
        now = self._clock()
        self._expire_global(now)
        waits = [0.0]
        if self._global_limit is not None and len(self._global_failures) >= self._global_limit:
            waits.append(self._global_failures[0] + self._global_window - now)
        failures, last_failure = self._client_failures(client, now)
        if failures >= self._free_failures:
            exponent = min(failures - self._free_failures, self._MAX_BACKOFF_EXPONENT)
            delay = min(2.0 ** exponent, self._max_delay)
            waits.append(last_failure + delay - now)
        return max(waits)

    def record_failure(self, client: str) -> None:
        now = self._clock()
        failures, _last_failure = self._client_failures(client, now)
        self._clients.pop(client, None)
        self._clients[client] = (failures + 1, now)
        if self._global_limit is not None:
            self._global_failures.append(now)
            self._expire_global(now)
        while len(self._clients) > self._max_tracked:
            self._clients.pop(next(iter(self._clients)))

    def record_success(self, client: str) -> None:
        self._clients.pop(client, None)

    def _client_failures(self, client: str, now: float) -> tuple[int, float]:
        failures, last_failure = self._clients.get(client, (0, 0.0))
        if failures and now - last_failure >= self._forget_after:
            self._clients.pop(client, None)
            return 0, 0.0
        return failures, last_failure

    def _expire_global(self, now: float) -> None:
        while self._global_failures and self._global_failures[0] <= now - self._global_window:
            self._global_failures.popleft()
