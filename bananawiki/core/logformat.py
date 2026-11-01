"""Log record formats shared by the wiki and the hosting service.

Standard library only: the ``banana`` controller logs with :func:`one_line` too.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

TEXT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# Control characters (C0 but line feed and tab, DEL, C1), Unicode line and paragraph separators and the
# bidirectional overrides, written as repr() would. A terminal acts on them (OSC 52 sets the clipboard, others
# retitle the window or rewrite lines), and a log viewer may break or reorder a line at them.
_ESCAPES = {code: f"\\x{code:02x}" for code in (*range(0x20), 0x7F, *range(0x80, 0xA0)) if chr(code) not in "\n\t"}
_ESCAPES.update({code: f"\\u{code:04x}" for code in (0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A))})
# The same characters as JSON escapes, for what json.dumps(ensure_ascii=False) leaves raw (it escapes C0 itself).
_JSON_ESCAPES = {code: f"\\u{code:04x}" for code in _ESCAPES}


def printable(value: object) -> str:
    """*value* as text a terminal shows as it is: every control character escaped, line feeds kept.

    For whatever the root operator's terminal or a log may show of a name someone else chose (a tenant's
    file, folder or database, a wiki, a user).
    """
    return str(value).translate(_ESCAPES)


def one_line(value: object) -> str:
    """:func:`printable` *value* without line breaks, so a logged value cannot forge further log lines."""
    return printable(str(value).replace("\r", " ").replace("\n", " "))


class TextFormatter(logging.Formatter):
    """:data:`TEXT_FORMAT` (or *fmt*) on one line, control characters and line feeds escaped; a traceback stays as it is.

    A logged value with a line feed (a percent-decoded request path, a name) would otherwise start a line that
    reads as a further, fully formatted record.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - the logging API's name
        return printable(super().formatMessage(record)).replace("\n", "\\n")


class StderrHandler(logging.StreamHandler):
    """Writes to the current ``sys.stderr``, like logging's last-resort handler, but through :class:`TextFormatter`."""

    def __init__(self, level: int = logging.WARNING, fmt: str = "%(message)s"):
        logging.Handler.__init__(self, level)
        self.setFormatter(TextFormatter(fmt))

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors, with every control character escaped (see :data:`_ESCAPES`)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False).translate(_JSON_ESCAPES)


def formatter(name: str) -> logging.Formatter:
    return JsonFormatter() if name == "json" else TextFormatter(TEXT_FORMAT)
