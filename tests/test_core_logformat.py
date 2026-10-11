from __future__ import annotations

import json
import logging

from bananawiki.core.logformat import JsonFormatter, formatter
from bananawiki.wiki.config import load_config


def _record(message, *args, exc_info=None):
    return logging.LogRecord("bananawiki.auth", logging.WARNING, __file__, 1, message, args, exc_info)


def test_json_lines_carry_time_level_logger_and_message():
    entry = json.loads(JsonFormatter().format(_record("Failed sign-in for %s", "alice")))
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "bananawiki.auth"
    assert entry["message"] == "Failed sign-in for alice"
    assert entry["time"].endswith("+00:00")
    assert "exception" not in entry


def test_json_lines_stay_on_one_line_with_a_traceback():
    try:
        raise ValueError("bad\nvalue")
    except ValueError as error:
        line = JsonFormatter().format(_record("Unhandled error", exc_info=(type(error), error, error.__traceback__)))
    assert "\n" not in line
    assert "ValueError: bad" in json.loads(line)["exception"]


def test_text_stays_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BW_INSTANCE_DIR", str(tmp_path))
    monkeypatch.delenv("BW_LOG_FORMAT", raising=False)
    assert load_config().log_format == "text"
    assert not isinstance(formatter("text"), JsonFormatter)
    monkeypatch.setenv("BW_LOG_FORMAT", "json")
    assert load_config().log_format == "json"
