import json
import logging
import sys

from app.logging_config import JsonFormatter, configure_logging


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_log_lines_are_json_with_extra_fields() -> None:
    line = JsonFormatter().format(_record("job finished", job_id=42, run_id="r1"))
    data = json.loads(line)
    assert data["msg"] == "job finished"
    assert data["level"] == "INFO"
    assert data["job_id"] == 42
    assert data["run_id"] == "r1"
    assert data["ts"].endswith("+00:00")


def test_exceptions_are_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in data["exc"]


def test_configuring_twice_adds_one_handler_and_keeps_others() -> None:
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(json_handlers) == 1
        assert other in root.handlers
        assert logging.getLogger("httpx2").level == logging.WARNING
    finally:
        root.removeHandler(other)
