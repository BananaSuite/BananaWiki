"""Rate limits shared by every portal worker (``hosting_rate_limit_hits``).

Without an explicit key a limit counts the client's network
(:func:`~bananawiki.core.web.client_network`): an IPv4 address, or an IPv6
address's whole /64, which one subscriber usually holds.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from flask import abort, request

from ..core.ratelimit import SqlLimiter
from ..core.web import client_network
from .db import _session


class HostingLimiter(SqlLimiter):
    TABLE = "hosting_rate_limit_hits"


def hit(bucket: str, limit: int, window: int, key: str | None = None) -> bool:
    """Record one hit; False when *key* (default: the client's network, see below) is over the limit."""
    return HostingLimiter(_session()).hit(key or client_network(), bucket, limit, window)


def exceeded(bucket: str, limit: int, window: int, key: str | None = None) -> bool:
    return HostingLimiter(_session()).exceeded(key or client_network(), bucket, limit, window)


def record(bucket: str, key: str | None = None) -> None:
    HostingLimiter(_session()).record(key or client_network(), bucket)


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


def clear(bucket: str, key: str | None = None) -> None:
    HostingLimiter(_session()).clear(key or client_network(), bucket)
