"""Places for large responses, so readers that take them slowly cannot hold every server thread.

Gunicorn's gthread workers (and waitress) write a response on the thread that
handled the request. The write timeout (:mod:`bananawiki.ops.write_timeout`)
ends a client that stops reading, but one that reads steadily and slowly (a
few KiB a second) holds the thread for as long as the body lasts: eight such
readers of one attachment left a default wiki without a thread for anything
else, ``/health`` included. Only small bodies fit in the kernel's buffers and
free the thread at once.

A response whose body is a stream or larger than :data:`LARGE_BODY` therefore
takes one of the process's *places* and one of its client's, both given back
once the server has closed the response. The server runs as many threads as
there are places on top of those for everything else (``BW_DOWNLOAD_THREADS``
in :mod:`bananawiki.ops.gunicorn_conf`), so short requests always find one.
Over a limit the client is answered at once (503 when the process has no
place left, 429 when the client holds its share) with ``Retry-After``.

One client holds at most a quarter of the places, so it takes four clients
reading slowly to fill a process. A client is the signed-in account, else
its network: counting accounts against their network as well would let one
school class behind one address share a single quarter. Views whose large
response costs real work (exports) take their place before they do it
(:func:`place_first`), so a refused export is neither built, counted against
its limit nor audited.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import Response, current_app, g, request

# Larger bodies may not fit in the socket buffers of a client that reads slowly (70 KB did, 300 KB did not).
LARGE_BODY = 64 * 1024
# The hosting portal's places, and the threads its workers run for them (hosting/gunicorn.conf.py): its large
# responses are wiki archives and platform exports, for owners and administrators. One each for four clients.
PORTAL_PLACES = 4
RETRY_AFTER = "5"
# app.extensions key of the application's Policy (see place_first).
POLICY = "bananawiki.download_policy"
# The flask.g attribute holding a place taken before the view ran.
_TAKEN = "bananawiki_download_place"


class Places:
    """*total* places in this process, at most *per_client* of them for one client (a quarter by default)."""

    def __init__(self, total: int, per_client: int | None = None):
        self.total = max(1, total)
        share = self.total // 4 if per_client is None else per_client
        self.per_client = max(1, min(share, self.total))
        self._lock = threading.Lock()
        self._active = 0
        self._clients: dict[str, int] = {}

    def take(self, client: str) -> tuple[Callable[[], None] | None, int]:
        """(its release function, safe to call twice, and 200), or (None, 503 or 429)."""
        with self._lock:
            if self._active >= self.total:
                return None, 503
            if self._clients.get(client, 0) >= self.per_client:
                return None, 429
            self._active += 1
            self._clients[client] = self._clients.get(client, 0) + 1
        released: list[bool] = []

        def release() -> None:
            with self._lock:
                if released:
                    return
                released.append(True)
                self._active -= 1
                left = self._clients.get(client, 1) - 1
                if left > 0:
                    self._clients[client] = left
                else:
                    self._clients.pop(client, None)

        return release, 200

    @property
    def active(self) -> int:
        return self._active


def large(response: Response) -> bool:
    """Whether writing *response* may hold its thread for as long as the client takes to read it."""
    if request.method == "HEAD" or response.status_code in (204, 304) or response.status_code < 200:
        return False
    length = response.content_length
    return length > LARGE_BODY if length is not None else response.is_streamed


def _on_close(response: Response, release: Callable[[], None]) -> None:
    if not response.direct_passthrough:
        response.call_on_close(release)
        return
    # A file sent with sendfile reaches the server as the file wrapper itself, whose close() the server calls; the
    # response's own close() is not called then.
    body = response.response
    close = getattr(body, "close", None)

    def closing() -> None:
        try:
            if close is not None:
                close()
        finally:
            release()

    try:
        body.close = closing  # type: ignore[union-attr]
    except AttributeError:
        release()  # not expected (the WSGI servers' wrappers allow it); never keep a place nothing gives back


def _refusal(refuse: Callable[[int], Response], status: int) -> Response:
    answer = refuse(status)
    answer.headers["Retry-After"] = RETRY_AFTER
    answer.headers["Cache-Control"] = "no-store"
    return answer


def hold(response: Response, places: Places | None, client: str,
         refuse: Callable[[int], Response]) -> Response:
    """*response*, holding a place until it is closed when it is :func:`large`; or *refuse* (status) without one.

    A place :func:`place_first` took for the request is the response's, or given back when the response is small.
    """
    taken = g.pop(_TAKEN, None)
    if places is None or not large(response):
        if taken is not None:
            taken()
        return response
    if taken is not None:
        release = taken
    else:
        release, status = places.take(client)
        if release is None:
            response.close()  # its files and temporary directories go now
            return _refusal(refuse, status)
    _on_close(response, release)
    return response


@dataclass(frozen=True)
class Policy:
    """How an application uses its places: the ``app.extensions`` key of its :class:`Places` (absent or None:
    no limit), who the client of the current request is, and its answer to a refused request (by status)."""

    extension: str
    client: Callable[[], str]
    refuse: Callable[[int], Response]


def place_first(view: Callable[..., Any]) -> Callable[..., Any]:
    """For views that do costly work for a large response (an export): the client's place is taken before the
    view runs, so a busy process or a client over its share is refused before the work is done, counted against
    a limit or audited. :func:`hold` then keeps the place for the response, or gives it back for a small one
    (a refusal, a redirect); :func:`give_back` if the request ends without a response."""

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        policy: Policy | None = current_app.extensions.get(POLICY)
        places = current_app.extensions.get(policy.extension) if policy is not None else None
        if places is not None and g.get(_TAKEN) is None:
            release, status = places.take(policy.client())
            if release is None:
                return _refusal(policy.refuse, status)
            setattr(g, _TAKEN, release)
        return view(*args, **kwargs)

    wrapper.download_place_first = True  # type: ignore[attr-defined]  # tests check the export views have it
    return wrapper


def give_back(_error: BaseException | None = None) -> None:
    """``teardown_request``: the place :func:`place_first` took, when no response kept it."""
    release = g.pop(_TAKEN, None)
    if release is not None:
        release()
