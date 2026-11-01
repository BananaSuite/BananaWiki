"""Gunicorn's access log with control characters escaped, like the application's own logs.

Gunicorn refuses C0 controls in header values but accepts bytes 0x80-0x9f, which it decodes as Latin-1, and it
writes the request line, the Referer, the User-Agent and every other atom unchanged. Those are the 8-bit forms of
CSI, OSC and ST, which a terminal or log viewer showing the log (journald, docker logs) may act on: any client
could set the window title or the clipboard. Every string atom goes through :func:`bananawiki.core.logformat.one_line`.
"""

from __future__ import annotations

from typing import Any

from gunicorn.glogging import Logger, SafeAtoms

from bananawiki.core.logformat import one_line


class EscapedAtoms(SafeAtoms):
    def __init__(self, atoms: dict[str, Any]) -> None:
        super().__init__({key: one_line(value) if isinstance(value, str) else value for key, value in atoms.items()})


class AccessLogger(Logger):
    """Set as ``logger_class`` by the wiki's and the portal's Gunicorn settings."""

    atoms_wrapper_class = EscapedAtoms
