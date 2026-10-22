from roborock_local_server.security import (
    AdminSessionManager,
    LoginThrottle,
    derive_protocol_sync_secret,
    hash_password,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    stored = hash_password("correct horse battery staple", iterations=10_000)

    assert verify_password("correct horse battery staple", stored) is True
    assert verify_password("not-it", stored) is False


def test_admin_session_verification_rejects_tampering() -> None:
    manager = AdminSessionManager(secret="abcdefghijklmnopqrstuvwxyz123456", ttl_seconds=3600)
    token = manager.issue()

    assert manager.verify(token) is not None
    assert manager.verify(token + "tampered") is None



def test_admin_session_binding_invalidates_cookies() -> None:
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    original = AdminSessionManager(secret=secret, ttl_seconds=3600, binding="old-password-hash")
    token = original.issue()

    assert AdminSessionManager(secret=secret, ttl_seconds=3600, binding="old-password-hash").verify(token) is not None
    assert AdminSessionManager(secret=secret, ttl_seconds=3600, binding="new-password-hash").verify(token) is None


def test_protocol_sync_secret_cannot_sign_admin_sessions() -> None:
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    sync_secret = derive_protocol_sync_secret(secret)
    manager = AdminSessionManager(secret=secret, ttl_seconds=3600)

    assert sync_secret != secret
    forged = AdminSessionManager(secret=sync_secret, ttl_seconds=3600).issue()
    assert manager.verify(forged) is None


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_login_throttle_backs_off_after_free_failures() -> None:
    clock = _FakeClock()
    throttle = LoginThrottle(free_failures=3, max_delay_seconds=60, clock=clock)

    for _ in range(3):
        assert throttle.retry_after("10.0.0.5") == 0
        throttle.record_failure("10.0.0.5")

    assert throttle.retry_after("10.0.0.5") == 1.0
    assert throttle.retry_after("10.0.0.6") == 0

    clock.now += 1.0
    assert throttle.retry_after("10.0.0.5") == 0
    throttle.record_failure("10.0.0.5")
    assert throttle.retry_after("10.0.0.5") == 2.0

    for _ in range(20):
        throttle.record_failure("10.0.0.5")
    assert throttle.retry_after("10.0.0.5") == 60

    throttle.record_success("10.0.0.5")
    assert throttle.retry_after("10.0.0.5") == 0


def test_login_throttle_global_budget_limits_distributed_guessing() -> None:
    clock = _FakeClock()
    throttle = LoginThrottle(global_failures_per_window=10, global_window_seconds=100, clock=clock)

    for index in range(10):
        throttle.record_failure(f"10.0.1.{index}")
        clock.now += 1.0

    assert throttle.retry_after("10.0.2.1") == 90.0
    clock.now += 90.0
    assert throttle.retry_after("10.0.2.1") == 0


def test_login_throttle_bounds_tracked_clients() -> None:
    throttle = LoginThrottle(max_tracked_clients=3, global_failures_per_window=1000, clock=_FakeClock())

    for index in range(10):
        throttle.record_failure(f"10.0.3.{index}")

    assert len(throttle._clients) == 3
