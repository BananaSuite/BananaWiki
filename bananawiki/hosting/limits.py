"""Rate limits shared by every portal worker (``hosting_rate_limit_hits``).

Without an explicit key a limit counts the client's network
(:func:`~bananawiki.core.web.client_network`): an IPv4 address, or an IPv6
address's whole /64, which one subscriber usually holds.

The server-to-server OAuth endpoints (``/oauth/token``, ``/oauth/link`` and
the others a wiki calls, marked :func:`server_to_server`) are not counted per
network: every call comes from the wiki's own address, on behalf of all of
its users, and its visitors can make it call the token endpoint with codes
they made up. While platform sign-in is on they are left out of the global
per-network limit (they answer 404 otherwise, and count against it like any
other path) and count instead what fails or what one wiki does:

* wrong client credentials and malformed requests (another ``grant_type``,
  no credentials), per network (:data:`BAD_CLIENT_PER_MINUTE`): a network
  over the limit is refused before the view reads anything of its next
  request (:func:`server_to_server`), so neither its body nor its credentials;
* unknown bearer tokens per network, and refused codes (or none) per wiki:
  over the limit such failures answer 429 after a read only, but a valid
  token or code is always taken;
* their bodies are capped at :data:`SERVER_TO_SERVER_BODY`: a wiki sends a
  small form or JSON object;
* authenticated calls per wiki, at most ``HOSTING_OAUTH_CLIENT_RATE`` a
  minute in each worker (:func:`client_call`). Exchanging a code is not
  counted: codes are random and single-use, and each needs a user's sign-in.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import abort, current_app, jsonify, request

from ..core.ratelimit import MemoryLimiter, SqlLimiter
from ..core.web import client_network
from .db import _session

CLIENT_LIMITER = "bananawiki.hosting.client_limiter"
BAD_CLIENT = "oauth_bad_client"
BAD_CLIENT_PER_MINUTE = 30
BAD_TOKEN = "oauth_bad_token"
BAD_TOKEN_PER_MINUTE = 60
BAD_GRANT = "oauth_bad_grant"
BAD_GRANT_PER_MINUTE = 60
SERVER_TO_SERVER_BODY = 64 * 1024


class HostingLimiter(SqlLimiter):
    TABLE = "hosting_rate_limit_hits"


def hit(bucket: str, limit: int, window: int, key: str | None = None) -> bool:
    """Record one hit; False when *key* (default: the client's network, see below) is over the limit."""
    return HostingLimiter(_session()).hit(key or client_network(), bucket, limit, window)


def exceeded(bucket: str, limit: int, window: int, key: str | None = None) -> bool:
    return HostingLimiter(_session()).exceeded(key or client_network(), bucket, limit, window)


def record(bucket: str, key: str | None = None) -> None:
    HostingLimiter(_session()).record(key or client_network(), bucket)


def failure(bucket: str, limit: int, window: int = 60, key: str | None = None) -> bool:
    """Count one failure of *key* (default: the client's network); False, writing nothing, once over the limit.

    Unlike :func:`hit`, the check is a plain read, so a flood over the limit never waits for the write lock.
    Concurrent failures may overshoot the limit by a few.
    """
    limiter = HostingLimiter(_session())
    key = key or client_network()
    if limiter.exceeded(key, bucket, limit, window):
        return False
    limiter.record(key, bucket)
    return True


def clients_refused() -> bool:
    """Whether this network sent too many wrong client credentials or malformed requests (:data:`BAD_CLIENT`)."""
    return exceeded(BAD_CLIENT, BAD_CLIENT_PER_MINUTE, 60)


# Wrong passwords on the pages that ask a signed-in account for its password again, from every network together:
# someone holding only the session cannot guess the password there faster than on /login.
CONFIRM_WINDOW = 15 * 60
CONFIRM_MAX_FAILURES = 20


def confirm_password(account: dict[str, Any], password: str) -> bool:
    """Check *account*'s password for a confirmation form; answer 429 once its failures are spent."""
    from .accounts import verify_password

    limiter = HostingLimiter(_session())
    reservation = limiter.reserve(f"id:{account['id']}", "confirm-password", CONFIRM_MAX_FAILURES, CONFIRM_WINDOW)
    if reservation is None:
        abort(429)
    if not verify_password(account, password):
        return False
    limiter.release(reservation)
    return True


def rate_limit(limit: int, window: int = 60, *, count_get: bool = False) -> Callable[[Callable], Callable]:
    """Limit unsafe requests to a view per client network.

    GET is not counted, unless *count_get*: for pages whose every view costs
    work elsewhere (a task in a wiki's container), so reloading them is limited too.
    """

    def decorate(view: Callable) -> Callable:
        bucket = f"hosting_{view.__module__.rsplit('.', 1)[-1]}_{view.__name__}"

        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if (count_get or request.method not in ("GET", "HEAD")) and not hit(bucket, limit, window):
                abort(429)
            return view(*args, **kwargs)

        return wrapper

    return decorate


def server_to_server(view: Callable | None = None, *, bearer: bool = False) -> Callable:
    """A view wikis call with their client credentials, or with a bearer token when *bearer* (see the module
    docstring).

    A network over its :data:`BAD_CLIENT` budget is refused before the view reads its request. Not for a
    bearer view: its token is a header, and a valid one is never refused for other requests' failures.
    """

    def decorate(view: Callable) -> Callable:
        wrapper = view
        if not bearer:
            @functools.wraps(view)
            def wrapper(*args: Any, **kwargs: Any):
                if clients_refused():
                    response = jsonify({"ok": False, "error": "too_many_requests"})
                    response.status_code = 429
                    response.headers["Cache-Control"] = "no-store"
                    return response
                return view(*args, **kwargs)

        wrapper._hosting_server_to_server = True  # type: ignore[attr-defined]
        return wrapper

    return decorate(view) if view is not None else decorate


def is_server_to_server(view: Callable | None) -> bool:
    return bool(getattr(view, "_hosting_server_to_server", False))


def client_call(client_id: str) -> bool:
    """Count one authenticated call of the wiki *client_id*; False above ``HOSTING_OAUTH_CLIENT_RATE``."""
    limiter: MemoryLimiter = current_app.extensions[CLIENT_LIMITER]
    return limiter.hit(client_id, current_app.config["HOSTING"].oauth_client_rate, 60)


def clear(bucket: str, key: str | None = None) -> None:
    HostingLimiter(_session()).clear(key or client_network(), bucket)
