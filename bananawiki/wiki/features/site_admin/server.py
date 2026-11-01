"""Server restart and the error log viewer.

A restart sends ``SIGHUP`` to the Gunicorn master that runs this worker:
Gunicorn then replaces its workers gracefully. It is only offered when the
request really is served by Gunicorn and the parent process is its master;
otherwise the page explains how to restart. A cooldown stored in
``site_settings.last_server_restart_at`` (claimed inside a transaction, so
concurrent workers cannot both restart) limits restarts to one a minute.
"""

from __future__ import annotations

import ipaddress
import os
import re
import signal
import stat
from pathlib import Path

from flask import current_app, request

from ....core.timeutil import now_sql, parse, utcnow
from ... import settings
from ...db import db

COOLDOWN_SECONDS = 60
LOG_TAIL_BYTES = 256 * 1024
_REDACTIONS = (
    (re.compile(r"(?i)(authorization:\s*\w+\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|cookie|session)(\w*)(\s*[=:]\s*)([^\s,;&'\"]+)"),
     r"\1\2\3[redacted]"),
)


def gunicorn_master_pid() -> int:
    """PID of the Gunicorn master serving this request, or 0 when there is none."""
    if not str(request.environ.get("SERVER_SOFTWARE", "")).lower().startswith("gunicorn"):
        return 0
    parent = os.getppid()
    if parent < 1:
        return 0
    cmdline = Path(f"/proc/{parent}/cmdline")
    if cmdline.exists():
        try:
            program = cmdline.read_bytes().split(b"\0")[:3]
            if not any(os.path.basename(arg).startswith(b"gunicorn") for arg in program):
                return 0
        except OSError:
            return 0
    elif parent == 1:
        # PID 1 is the master only when /proc shows Gunicorn (a container
        # without an init); otherwise it may be an init that adopted this worker.
        return 0
    return parent


def cooldown_remaining() -> int:
    last = parse(settings.get("last_server_restart_at"))
    if last is None:
        return 0
    return max(0, COOLDOWN_SECONDS - int((utcnow() - last).total_seconds()))


def claim_restart() -> bool:
    """Record a restart now unless one happened within the cooldown."""
    with db.transaction():
        row = db.one("SELECT last_server_restart_at FROM site_settings WHERE id = 1") or {}
        last = parse(row.get("last_server_restart_at"))
        if last is not None and (utcnow() - last).total_seconds() < COOLDOWN_SECONDS:
            return False
        db.execute("UPDATE site_settings SET last_server_restart_at = ? WHERE id = 1", (now_sql(),))
    settings.invalidate()
    return True


def restart(master_pid: int) -> None:
    os.kill(master_pid, signal.SIGHUP)


# Error log


def _secrets() -> list[str]:
    cfg = current_app.config["BW"]
    values = [cfg.secret_key, cfg.setup_token, *cfg.platform_oauth.values()]
    return [value for value in values if value and len(value) >= 8]


def redact(text: str) -> str:
    for secret in _secrets():
        text = text.replace(secret, "[redacted]")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# Runs of characters an IPv4 or IPv6 address is made of, and IPv4 addresses inside a longer run (with a port).
# Possessive (no backtracking): the log may hold a long run of these characters.
_ADDRESS_RUN = re.compile(r"(?<![\w.:])[0-9A-Fa-f:.]++(?![\w.:])")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
ADDRESS_PLACEHOLDER = "[address]"


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def hide_addresses(text: str) -> str:
    """*text* with every IPv4 and IPv6 address replaced by :data:`ADDRESS_PLACEHOLDER`."""
    def run(match: re.Match[str]) -> str:
        found = match.group(0)
        if "." not in found and ":" not in found:
            return found
        core = found.rstrip(".:")  # the end of a sentence or a "from x: reason"
        if _is_address(core):
            return ADDRESS_PLACEHOLDER + found[len(core):]
        return _IPV4.sub(lambda m: ADDRESS_PLACEHOLDER if _is_address(m.group(0)) else m.group(0), found)
    return _ADDRESS_RUN.sub(run, text)


def log_tail(errors_only: bool = False, *, addresses: bool = True) -> str | None:
    """The end of the configured log file, redacted; None when there is no readable log.

    Without *addresses* the client addresses are hidden too: the log names who
    failed to sign in from where, owners and superusers included, whose
    addresses plain administrators are not shown anywhere else.
    """
    path = current_app.config["BW"].log_file
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - LOG_TAIL_BYTES)
        handle.seek(start)
        text = handle.read(LOG_TAIL_BYTES).decode("utf-8", errors="replace")
    if start:
        text = text.split("\n", 1)[-1]
    if errors_only:
        text = _error_entries(text)
    text = redact(text)
    return text if addresses else hide_addresses(text)


_ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _error_entries(text: str) -> str:
    """Keep ERROR and CRITICAL entries with their tracebacks."""
    kept, keep = [], False
    for line in text.splitlines():
        if _ENTRY_START.match(line):
            keep = " ERROR " in line or " CRITICAL " in line
        if keep:
            kept.append(line)
    return "\n".join(kept)
