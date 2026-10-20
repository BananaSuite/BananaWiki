"""Certificate warm-up: the first TLS handshake for a new wiki host, made by the portal.

Caddy orders a wiki's certificate inside the first handshake it sees for the
host (on-demand TLS), so the first visitor would wait for the CA. After the
routes are published the portal makes that handshake itself, in the background,
and tries again for a few minutes while Caddy is still reloading or the CA is
slow. It is best effort: it never blocks a request, raises into the caller or
changes what is served.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable

from flask import current_app

log = logging.getLogger("bananawiki.hosting.certwarm")

HTTPS_PORT = 443
ATTEMPT_TIMEOUT = 100
RETRY_DELAY = 5
GIVE_UP_AFTER = 300

_active: set[str] = set()
_lock = threading.Lock()


def handshake(host: str, port: int, timeout: float) -> None:
    """A TLS handshake with the local Caddy for *host*; the certificate is not checked, only obtained."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as plain, \
            context.wrap_socket(plain, server_hostname=host):
        pass


connect: Callable[[str, int, float], None] = handshake


def warm(host: str, port: int = HTTPS_PORT) -> bool:
    """Handshake until it works or ``GIVE_UP_AFTER`` seconds have passed; logs the outcome once."""
    started = time.monotonic()
    delay = RETRY_DELAY
    while True:
        try:
            connect(host, port, ATTEMPT_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - best effort, whatever the connector raises
            if time.monotonic() - started + delay >= GIVE_UP_AFTER:
                log.warning("No certificate for %s after %d seconds (%s).", host,
                            time.monotonic() - started, type(error).__name__)
                return False
            time.sleep(delay)
            # A failed handshake can be a failed order at the CA, which allows 5 failed validations per host and hour.
            delay *= 2
        else:
            log.info("Certificate for %s is ready.", host)
            return True


def _done(host: str) -> None:
    with _lock:
        _active.discard(host)


def _run(host: str, port: int) -> None:
    try:
        warm(host, port)
    finally:
        _done(host)


def schedule(host: str) -> threading.Thread | None:
    """Warm *host* in a daemon thread, unless the platform does not serve HTTPS itself or it is already running."""
    cfg = current_app.config["HOSTING"]
    host = (host or "").strip().lower()
    if not host or cfg.testing or cfg.hosting_mode != "subdomain":
        return None
    with _lock:
        if host in _active:
            return None
        _active.add(host)
    thread = threading.Thread(target=_run, args=(host, HTTPS_PORT), name=f"certwarm-{host}", daemon=True)
    try:
        thread.start()
    except RuntimeError:  # no thread to spare: the first visitor waits instead
        _done(host)
        return None
    return thread
