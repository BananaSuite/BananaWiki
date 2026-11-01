"""Gunicorn settings of the desktop launcher's child, beyond its command line.

Passed with ``-c`` so gunicorn never reads a ``gunicorn.conf.py`` from a
working directory (see :class:`bananawiki.desktop.server._GunicornRunner`).
It imports BananaWiki's own packaged modules only: the data folder is never
on the child's path.

``post_worker_init`` installs the write timeout of
:mod:`bananawiki.ops.write_timeout` (``BW_WRITE_TIMEOUT``, 300 s unless the
launcher passes it): Gunicorn has none of its own, so a device on the network
that starts a download and stops reading would hold its thread, and its
download place, until the wiki is stopped. It also installs the request
header deadline and the cap per address of :mod:`.gunicorn_limits`: no proxy
sits in front of the child, and gunicorn reads request headers with no
timeout, so a device sending them slowly would hold every thread.
"""

from bananawiki.desktop import server as _server
from bananawiki.desktop.gunicorn_limits import HeaderGuard as _HeaderGuard
from bananawiki.desktop.gunicorn_limits import log as _log
from bananawiki.ops.gunicorn_conf import post_worker_init as _write_timeout
from bananawiki.ops.write_timeout import patch_graceful_close as _patch_graceful_close

# One request per connection, closed without waiting for the client (see bananawiki/ops/gunicorn_conf.py).
keepalive = 0
_patch_graceful_close()


def post_worker_init(worker) -> None:
    """The standalone wiki's write timeout, and the request header limits (see the module docstring)."""
    from gunicorn.workers.gthread import ThreadWorker

    _write_timeout(worker)
    if isinstance(worker, ThreadWorker):  # always: the launcher passes --threads, which selects gthread
        _HeaderGuard(_server.HEADER_TIMEOUT, _server.HEADER_READERS_PER_ADDRESS).install(worker)
    else:
        _log.warning("Not a gthread worker: request headers have no deadline")
