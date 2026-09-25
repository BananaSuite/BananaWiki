#!/usr/bin/env python
"""Start the BananaWiki managed hosting portal (development server).

Usage:
    From the hosting/ directory:  python run.py
    From the repository root:     python -m hosting

    With Gunicorn (production):
        gunicorn hosting.wsgi:app --bind 127.0.0.1:5099
"""

import os
import sys

# Ensure the repository root (parent of hosting/) is on sys.path so
# the hosting package is importable when this script is executed
# directly from inside the hosting/ directory.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hosting.app import create_hosting_app  # noqa: E402
from hosting import config  # noqa: E402

app = create_hosting_app()
app.run(
    host=config.HOSTING_HOST,
    port=config.HOSTING_PORT,
    debug=os.environ.get("HOSTING_DEBUG", "").lower() in ("1", "true", "yes"),
)
