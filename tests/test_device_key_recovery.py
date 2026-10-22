import base64
import json
from pathlib import Path
import threading
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from roborock_local_server.bundled_backend.shared import device_key_recovery


def test_device_key_cache_retries_saved_two_sample_recovery_on_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    resumed: list[str] = []

    def fake_maybe_recover_async(self, did: str) -> None:
        resumed.append(did)

    monkeypatch.setattr(
        device_key_recovery.DeviceKeyCache,
        "maybe_recover_async",
        fake_maybe_recover_async,
    )

    state_path = tmp_path / "device_key_state.json"
    state_path.write_text(
        json.dumps(
            {
                "devices": {
                    "1234567890123": {
                        "samples": [
                            {
                                "canonical": "foo=bar",
                                "signature_b64": "QUJD",
                            },
                            {
                                "canonical": "baz=qux",
                                "signature_b64": "REVG",
                            }
                        ],
                        "recovery": {
                            "state": "blocked",
                            "note": "Legacy state from before gmpy2 was required.",
                            "started_at": "2026-03-15T20:55:21.514097+00:00",
                        },
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    device_key_recovery.DeviceKeyCache(state_path)

    assert resumed == ["1234567890123"]


def _signature(length: int, seed: int = 1) -> str:
    return base64.b64encode(bytes([seed % 256]) * length).decode("ascii")


@pytest.mark.parametrize("length", [3, 32, 64, 127, 129, 255, 384, 1024])
def test_implausible_signature_lengths_are_not_collected(tmp_path: Path, length: int) -> None:
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    assert not cache.add_signed_query("did-a", f"a=1&signature={_signature(length)}")
    assert not cache.add_header_signature(
        "did-a", method="GET", path="/region", query="a=1", nonce="n", ts="1", signature_b64=_signature(length)
    )
    assert cache.get_known_dids() == []
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("length", [128, 256, 512])
def test_rsa_signature_lengths_are_collected(tmp_path: Path, length: int) -> None:
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    assert cache.add_signed_query("did-a", f"a=1&signature={_signature(length)}")
    assert cache.add_header_signature(
        "did-a", method="GET", path="/region", query="a=1", nonce="n", ts="1", signature_b64=_signature(length)
    )
    assert cache.get_known_dids() == ["did-a"]


def test_new_dids_are_dropped_once_collecting_limit_is_reached(tmp_path: Path) -> None:
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    limit = device_key_recovery.MAX_COLLECTING_DIDS
    for index in range(limit):
        assert cache.add_signed_query(f"did-{index}", f"a=1&signature={_signature(256)}")

    assert not cache.add_signed_query("did-new", f"a=1&signature={_signature(256)}")
    assert not cache.add_header_signature(
        "did-new", method="GET", path="/region", query="a=1", nonce="n", ts="1", signature_b64=_signature(512)
    )
    assert "did-new" not in cache.get_known_dids()
    # Dids already collecting keep collecting; the onboarding target bypasses the limit.
    assert cache.add_signed_query("did-0", f"a=2&signature={_signature(256)}")
    assert cache.add_signed_query("did-target", f"a=1&signature={_signature(256)}", exempt_did_limit=True)

    # Dids with a recovered key no longer count towards the limit (the exempt target does).
    with cache._lock:
        cache._set_modulus_locked("did-1", _rsa_modulus())
    assert not cache.add_signed_query("did-later", f"a=1&signature={_signature(256)}")
    with cache._lock:
        cache._set_modulus_locked("did-2", _rsa_modulus())
    assert cache.add_signed_query("did-later", f"a=1&signature={_signature(256)}")


def test_legacy_short_signature_dids_do_not_count_towards_limit(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    devices = {
        f"legacy-{index}": {"samples": [{"canonical": "a=1", "signature_b64": "QUJD"}]}
        for index in range(device_key_recovery.MAX_COLLECTING_DIDS + 2)
    }
    state_path.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    cache = device_key_recovery.DeviceKeyCache(state_path)
    assert cache.add_signed_query("did-new", f"a=1&signature={_signature(256)}")


def test_samples_per_did_are_capped(tmp_path: Path) -> None:
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    for index in range(40):
        assert cache.add_signed_query("did-a", f"a={index}&signature={_signature(256)}")
        assert cache.add_header_signature(
            "did-a",
            method="GET",
            path="/region",
            query=f"a={index}",
            nonce=f"n{index}",
            ts="1",
            signature_b64=_signature(512),
        )
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["devices"]["did-a"]
    assert len(saved["samples"]) == device_key_recovery.MAX_QUERY_SAMPLE_COUNT
    assert len(saved["header_samples"]) == device_key_recovery.MAX_HEADER_SAMPLE_COUNT
    assert saved["samples"][-1]["canonical"] == "a=39"


def test_no_samples_are_collected_after_key_is_recovered(tmp_path: Path) -> None:
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    with cache._lock:
        cache._set_modulus_locked("did-a", _rsa_modulus())
    assert not cache.add_signed_query("did-a", f"a=1&signature={_signature(256)}")
    assert not cache.add_header_signature(
        "did-a", method="GET", path="/region", query="a=1", nonce="n", ts="1", signature_b64=_signature(512)
    )


def test_startup_resumes_a_bounded_number_of_recoveries(tmp_path: Path, monkeypatch) -> None:
    resumed: list[str] = []
    monkeypatch.setattr(
        device_key_recovery.DeviceKeyCache, "maybe_recover_async", lambda self, did: resumed.append(did)
    )
    state_path = tmp_path / "state.json"
    devices = {
        f"did-{index}": {
            "samples": [
                {"canonical": "a=1", "signature_b64": _signature(256, 1)},
                {"canonical": "a=2", "signature_b64": _signature(256, 2)},
            ]
        }
        for index in range(6)
    }
    state_path.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    original = state_path.read_text(encoding="utf-8")

    cache = device_key_recovery.DeviceKeyCache(state_path)

    assert resumed == ["did-0", "did-1"][: device_key_recovery.MAX_RESUMED_RECOVERIES]
    assert len(resumed) == device_key_recovery.MAX_RESUMED_RECOVERIES
    # Loading must not rewrite or drop existing samples.
    assert state_path.read_text(encoding="utf-8") == original
    assert cache.get_known_dids() == sorted(devices)


def _rsa_modulus() -> int:
    return rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key().public_numbers().n


class _InlineRecoveryContext:
    """Stand-in for the spawn context: runs the recovery target in the worker thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started: list[str] = []

    def Pipe(self, duplex: bool = False):  # noqa: N802 - mirrors multiprocessing API
        box: list = []

        class _End:
            def send(self, payload) -> None:
                box.append(payload)

            def recv(self):
                return box.pop(0)

            def poll(self) -> bool:
                return bool(box)

            def close(self) -> None:
                pass

        return _End(), _End()

    def Process(self, *, target, args, daemon):  # noqa: N802 - mirrors multiprocessing API
        ctx = self

        class _Process:
            exitcode = 0

            def start(self) -> None:
                with ctx.lock:
                    ctx.running += 1
                    ctx.max_running = max(ctx.max_running, ctx.running)
                try:
                    target(*args)
                finally:
                    with ctx.lock:
                        ctx.running -= 1

            def join(self) -> None:
                pass

        return _Process()


def _wait_for_recovery_threads(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name.startswith("key-recover-"):
            thread.join(max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive()


def _two_samples(cache, did: str) -> None:
    assert cache.add_signed_query(did, f"a=1&signature={_signature(256, 1)}")
    assert cache.add_signed_query(did, f"a=2&signature={_signature(256, 2)}")


def test_only_one_recovery_process_runs_at_a_time(tmp_path: Path, monkeypatch) -> None:
    ctx = _InlineRecoveryContext()
    monkeypatch.setattr(device_key_recovery, "_MP_CTX", ctx)
    release = threading.Event()
    calls: list[int] = []

    def fake_recover(samples, *, e, hash_name, diagnostics):
        calls.append(len(samples))
        release.wait(10)
        return None

    monkeypatch.setattr(device_key_recovery, "recover_modulus_from_samples", fake_recover)
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    for did in ("did-a", "did-b", "did-c"):
        _two_samples(cache, did)
        cache.maybe_recover_async(did)

    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert ctx.running == 1
    finally:
        release.set()
    _wait_for_recovery_threads()

    assert ctx.max_running == 1
    assert len(calls) == 3


def test_recovery_never_replaces_an_existing_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(device_key_recovery, "_MP_CTX", _InlineRecoveryContext())
    entered = threading.Event()
    release = threading.Event()
    recovered = _rsa_modulus()
    existing = _rsa_modulus()

    def fake_recover(samples, *, e, hash_name, diagnostics):
        entered.set()
        release.wait(10)
        return recovered

    monkeypatch.setattr(device_key_recovery, "recover_modulus_from_samples", fake_recover)
    cache = device_key_recovery.DeviceKeyCache(tmp_path / "state.json")
    _two_samples(cache, "did-a")
    cache.maybe_recover_async("did-a")
    assert entered.wait(5)
    with cache._lock:
        cache._set_modulus_locked("did-a", existing)
    release.set()
    _wait_for_recovery_threads()

    assert cache.get_pubkey("did-a").public_numbers().n == existing
