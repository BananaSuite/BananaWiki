"""WSGI entry point kept at the repository root for ``gunicorn -c gunicorn.conf.py wsgi:app``.

Managed servers and container images run exactly this command (the 1.4
updater writes it into the systemd unit), so the name must not change.
"""

from bananawiki.ops.wsgi import app, application

__all__ = ["app", "application"]
