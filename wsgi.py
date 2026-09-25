"""
WSGI entry point for BananaWiki.

Usage with Gunicorn:
    gunicorn wsgi:app -c gunicorn.conf.py

Or with default settings:
    gunicorn wsgi:app --bind 0.0.0.0:5001 --workers 2
"""

from app import app  # noqa: F401
from banana_ops.gate import MaintenanceGate

app.wsgi_app = MaintenanceGate(app.wsgi_app)
