from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _service() -> dict:
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return compose["services"]["roborock-local-server"]


def test_compose_blocks_privilege_escalation() -> None:
    assert "no-new-privileges:true" in _service()["security_opt"]


def test_compose_drops_all_capabilities_except_required_ones() -> None:
    service = _service()
    assert service["cap_drop"] == ["ALL"]
    # Mosquitto privilege drop (SETUID/SETGID) and stopping it (KILL), host-owned
    # bind mounts (DAC_OVERRIDE), ports below 1024 outside bridge networking.
    assert sorted(service["cap_add"]) == ["DAC_OVERRIDE", "KILL", "NET_BIND_SERVICE", "SETGID", "SETUID"]


def test_compose_keeps_root_user_for_existing_data_ownership() -> None:
    assert "user" not in _service()
