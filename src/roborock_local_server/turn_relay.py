"""Manage the optional coturn relay shipped with the container."""

from __future__ import annotations

from pathlib import Path
import ipaddress
import logging
import socket
import subprocess
import time

from .bundled_backend.shared.io_utils import atomic_write_text
from .config import TurnConfig


LOG = logging.getLogger(__name__)

RELAY_PORT_MIN = 49160
RELAY_PORT_MAX = 49179

# Peers coturn refuses to relay to. Without these an authenticated TURN client could use
# the relay to reach services inside this container (the embedded broker, the plain
# listeners) or the host's loopback-only ports. Private (RFC 1918) and CGNAT/Tailscale
# (100.64.0.0/10) ranges are deliberately left open: the robot and the camera viewer are
# normally on the LAN or a tailnet, and ICE may pair the robot's relay candidate with the
# viewer's host candidate there. The trade-off is that a relay client can still reach
# other LAN hosts; list ranges no viewer uses in ``turn.denied_peer_ips`` to close them.
DENIED_PEER_NETWORKS = (
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "fe80::/10",
    "ff00::/8",
)


def _peer_ip_range(entry: str) -> str:
    network = ipaddress.ip_network(entry, strict=False)
    first, last = network[0], network[-1]
    return str(first) if first == last else f"{first}-{last}"


def _local_ipv4_addresses() -> set[str]:
    """Addresses of this container/host, resolved the way Docker publishes them in /etc/hosts."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return set()
    return {str(info[4][0]) for info in infos if not ipaddress.ip_address(info[4][0]).is_loopback}


def denied_peer_lines(*, external_ip: str, extra: tuple[str, ...] = ()) -> list[str]:
    entries = list(DENIED_PEER_NETWORKS)
    # The container's own address exposes its internal listeners. With host networking it
    # is also the advertised relay address that viewers and relay-to-relay paths use, so
    # it stays allowed in that case.
    entries.extend(sorted(_local_ipv4_addresses() - {external_ip}))
    entries.extend(extra)
    lines: list[str] = []
    for entry in entries:
        line = f"denied-peer-ip={_peer_ip_range(entry)}"
        if line not in lines:
            lines.append(line)
    return lines


class EmbeddedTurnRelay:
    def __init__(self, config: TurnConfig, state_dir: Path) -> None:
        self.config = config
        self.state_dir = state_dir
        self.process: subprocess.Popen[bytes] | None = None
        self._exit_warned = False

    @property
    def running(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is not None and not self._exit_warned:
            LOG.warning("coturn stopped with exit code %s; see container stdout", code)
            self._exit_warned = True
        return code is None

    def start(self) -> None:
        if self.config.mode != "provided":
            return
        addresses = socket.getaddrinfo(self.config.host, self.config.port, socket.AF_INET, socket.SOCK_DGRAM)
        external_ip = addresses[0][4][0]
        LOG.info("TURN advertised IPv4 address resolved from %s: %s", self.config.host, external_ip)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.state_dir / "turnserver.conf"
        atomic_write_text(config_path, "\n".join([
            f"listening-port={self.config.port}",
            f"external-ip={external_ip}",
            f"min-port={RELAY_PORT_MIN}",
            f"max-port={RELAY_PORT_MAX}",
            "lt-cred-mech",
            f"realm={self.config.realm}",
            f"user={self.config.username}:{self.config.password}",
            "fingerprint",
            "no-tcp",
            "no-tls",
            "no-dtls",
            "no-multicast-peers",
            *denied_peer_lines(external_ip=external_ip, extra=self.config.denied_peer_ips),
            "no-cli",
            "no-rfc5780",
            "simple-log",
            "log-file=stdout",
            "",
        ]))
        self.process = subprocess.Popen(
            ["turnserver", "-c", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=subprocess.STDOUT,
        )
        self._exit_warned = False
        time.sleep(0.3)
        if self.process.poll() is not None:
            self.stop()
            raise RuntimeError("coturn exited during startup; see container stdout")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
