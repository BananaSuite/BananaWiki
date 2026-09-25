"""Allow running the hosting portal with ``python -m hosting``.

Usage (from the repository root):
    python -m hosting
"""

import os

from .app import create_hosting_app
from . import config

app = create_hosting_app()
app.run(
    host=config.HOSTING_HOST,
    port=config.HOSTING_PORT,
    debug=os.environ.get("HOSTING_DEBUG", "").lower() in ("1", "true", "yes"),
)
