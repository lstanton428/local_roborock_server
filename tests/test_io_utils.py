import logging
from pathlib import Path

from roborock_local_server.bundled_backend.shared.io_utils import append_jsonl, setup_file_logger


def test_setup_file_logger_mirrors_messages_to_file_and_stream(
    tmp_path: Path,
    capsys,
) -> None:
    log_path = tmp_path / "api_server.log"
    logger = setup_file_logger("api_test", log_path)

    logger.info("device initialization failed")

    captured = capsys.readouterr()
    assert "device initialization failed" in captured.err
    assert "[real_stack.api_test]" in captured.err
    assert "device initialization failed" in log_path.read_text(encoding="utf-8")


def test_append_jsonl_writes_file_and_hides_structured_entry_by_default(
    tmp_path: Path,
    capsys,
) -> None:
    logger = logging.getLogger("real_stack.jsonl")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)

    jsonl_path = tmp_path / "decompiled_http.jsonl"
    append_jsonl(jsonl_path, {"route": "get_home_data", "success": True})

    captured = capsys.readouterr()
    written = jsonl_path.read_text(encoding="utf-8")

    assert '"route":"get_home_data"' in written
    assert captured.err == ""


def test_append_jsonl_streams_structured_entry_when_jsonl_logger_is_debug(
    tmp_path: Path,
    capsys,
) -> None:
    logger = logging.getLogger("real_stack.jsonl")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    jsonl_path = tmp_path / "decompiled_http.jsonl"
    append_jsonl(jsonl_path, {"route": "get_home_data", "success": True})

    captured = capsys.readouterr()

    assert "[decompiled_http.jsonl]" in captured.err
    assert '"success":true' in captured.err


def test_setup_file_logger_keeps_each_entry_on_lines_it_owns(tmp_path: Path) -> None:
    log_path = tmp_path / "mqtt_server.log"
    logger = setup_file_logger("api_test_newlines", log_path)

    forged = "2026-01-01 00:00:00,000 [INFO] [conn 1 b2c] CONNACK len=4 hex=20020000"
    logger.warning("rejected username=%s", f"user\n{forged}\r{forged}")
    logger.info("next entry")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(line.startswith("\t") for line in lines[1:3])
    assert lines[3].endswith("[INFO] next entry")
    assert not any(line.startswith("2026-01-01") for line in lines)
