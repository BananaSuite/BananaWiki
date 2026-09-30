"""WSGI entry point for the wiki (``bananawiki.ops.wsgi:app``; the root ``wsgi.py`` re-exports it).

``create_app`` reads the configuration from ``BW_*`` variables, migrates the
database and installs the maintenance gate that answers 503 while
``BANANA_MAINTENANCE_FILE`` exists (``/health`` stays available for the
updater's readiness check).
"""

from __future__ import annotations

from bananawiki.wiki.app import create_app

app = create_app()
application = app
