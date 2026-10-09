"""Gunicorn settings for ``gunicorn -c gunicorn.conf.py wsgi:app`` (see bananawiki/ops/gunicorn_conf.py).

Reads BW_HOST, BW_PORT, BW_WORKERS, BW_THREADS, BW_WORKER_TIMEOUT,
BW_WRITE_TIMEOUT and BW_ACCESS_LOG; heartbeats go to /dev/shm when it is writable.
"""

from bananawiki.ops.gunicorn_conf import *  # noqa: F403
