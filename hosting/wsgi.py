"""WSGI entry point for ``gunicorn -c hosting/gunicorn.conf.py hosting.wsgi:app``."""

from bananawiki.hosting import create_app

app = create_app()
application = app

__all__ = ["app", "application"]
