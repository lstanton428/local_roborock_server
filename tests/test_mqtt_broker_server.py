import asyncio
import logging

from roborock_local_server.bundled_backend.mqtt_broker_server import server as broker_module
from roborock_local_server.bundled_backend.mqtt_broker_server.server import MosquittoBroker


class _RunningProcess:
    stdout = None

    def poll(self) -> None:
        return None


def test_ready_log_names_the_loopback_address_mosquitto_listens_on(tmp_path, monkeypatch, caplog) -> None:
    probed: list[tuple[str, int]] = []
    monkeypatch.setattr(broker_module, "resolve_mosquitto_binary", lambda binary: "/usr/sbin/mosquitto")
    monkeypatch.setattr(broker_module.subprocess, "Popen", lambda *args, **kwargs: _RunningProcess())
    monkeypatch.setattr(MosquittoBroker, "_is_port_open", staticmethod(lambda host, port: probed.append((host, port)) or True))
    broker = MosquittoBroker(port=18830, state_dir=tmp_path, logger=logging.getLogger("test_broker"))

    with caplog.at_level(logging.INFO, logger="test_broker"):
        asyncio.run(broker.start())

    assert "listener 18830 127.0.0.1" in broker.config_path.read_text(encoding="utf-8")
    assert probed == [("127.0.0.1", 18830)]
    assert "Mosquitto ready on 127.0.0.1:18830 using /usr/sbin/mosquitto" in caplog.messages
    assert not any("0.0.0.0" in message for message in caplog.messages)
