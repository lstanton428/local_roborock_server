import subprocess

import pytest

from roborock_local_server.config import TurnConfig
from roborock_local_server.turn_relay import EmbeddedTurnRelay
from roborock_local_server import turn_relay


@pytest.mark.parametrize("mode", ["disabled", "external"])
def test_other_modes_do_not_start_coturn(tmp_path, monkeypatch, mode):
    def unexpected(*args, **kwargs):
        pytest.fail("coturn should not start")
    monkeypatch.setattr(turn_relay.subprocess, "Popen", unexpected)
    relay = EmbeddedTurnRelay(TurnConfig(mode=mode), tmp_path)
    relay.start()
    assert not relay.running


def test_failed_coturn_launch_does_not_leave_process(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_relay.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("192.0.2.1", 3478))])
    def missing(*args, **kwargs):
        raise FileNotFoundError("coturn unavailable")
    monkeypatch.setattr(turn_relay.subprocess, "Popen", missing)
    relay = EmbeddedTurnRelay(TurnConfig(mode="provided", host="turn.example.com", username="user", password="secret", realm="example.com"), tmp_path)
    with pytest.raises(FileNotFoundError):
        relay.start()
    assert not relay.running


def test_stop_kills_relay_that_ignores_termination(tmp_path):
    class Process:
        terminated = False
        killed = False
        def terminate(self):
            self.terminated = True
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("turnserver", timeout)
        def kill(self):
            self.killed = True
    relay = EmbeddedTurnRelay(TurnConfig(), tmp_path)
    process = Process()
    relay.process = process
    relay.stop()
    assert process.terminated and process.killed
    assert relay.process is None


def test_provided_coturn_config_denies_internal_peers(tmp_path, monkeypatch):
    class Process:
        def poll(self):
            return None
    monkeypatch.setattr(turn_relay.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("192.0.2.1", 3478))])
    # Container address plus the advertised address (host networking): only the former is denied.
    monkeypatch.setattr(turn_relay, "_local_ipv4_addresses", lambda: {"172.17.0.2", "192.0.2.1"})
    monkeypatch.setattr(turn_relay.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(turn_relay.time, "sleep", lambda _seconds: None)
    relay = EmbeddedTurnRelay(TurnConfig(mode="provided", host="turn.example.com", username="user", password="secret",
        realm="example.com", denied_peer_ips=("10.20.0.0/16",)), tmp_path)
    relay.start()
    lines = (tmp_path / "turnserver.conf").read_text().splitlines()
    denied = {line.split("=", 1)[1] for line in lines if line.startswith("denied-peer-ip=")}
    assert {
        "0.0.0.0-0.255.255.255",
        "127.0.0.0-127.255.255.255",
        "169.254.0.0-169.254.255.255",
        "224.0.0.0-239.255.255.255",
        "::1",
        "fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "172.17.0.2",
        "10.20.0.0-10.20.255.255",
    } <= denied
    assert "192.0.2.1" not in denied
    # LAN and tailnet viewers stay reachable through the relay.
    assert not any(entry.startswith(("192.168.", "100.64.")) for entry in denied)
