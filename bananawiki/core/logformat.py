"""Log record formats shared by the wiki and the hosting service.

Standard library only: the ``banana`` controller logs with :func:`one_line` too.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

TEXT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def one_line(value: object) -> str:
    """*value* as text without line breaks, so a logged value cannot forge further log lines."""
    return str(value).replace("\r", " ").replace("\n", " ")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def formatter(name: str) -> logging.Formatter:
    return JsonFormatter() if name == "json" else logging.Formatter(TEXT_FORMAT)
