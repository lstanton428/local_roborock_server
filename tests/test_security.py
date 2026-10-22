from roborock_local_server.security import (
    AdminSessionManager,
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

