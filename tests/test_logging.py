import json
import logging
import sys

import pytest

from app.logging_config import JsonFormatter, mask_email


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


@pytest.mark.parametrize(
    ("email", "masked"),
    [("jane.doe@acme.com", "j***@acme.com"), ("x@y.io", "x***@y.io"), ("not-an-email", "***")],
)
def test_mask_email(email: str, masked: str) -> None:
    assert mask_email(email) == masked
