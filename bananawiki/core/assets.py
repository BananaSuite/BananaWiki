"""Static assets shared by the wiki and hosting portal.

The design system has one source in ``core/static``. Its public URL stays
``/static/css/bananawiki.css``; application and plugin assets keep their
own static folders and Flask endpoints.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from flask import Flask, Response, current_app, send_from_directory, url_for

from .. import __version__

SHARED_STATIC_ROOT = Path(__file__).parent / "static"
SHARED_FILENAMES = frozenset({"css/bananawiki.css"})


class SharedAssetsFlask(Flask):
    """Serve shared files through Flask's ordinary ``static`` endpoint."""

    def send_static_file(self, filename: str) -> Response:
        if filename in SHARED_FILENAMES:
            return send_from_directory(
                SHARED_STATIC_ROOT, filename, max_age=self.get_send_file_max_age(filename),
            )
        return super().send_static_file(filename)


@lru_cache(maxsize=512)
def _file_hash(path: Path, mtime_ns: int, size: int) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def asset_url(filename: str, *, blueprint: str | None = None) -> str:
    """Version the same source that the static endpoint will serve."""
    endpoint = f"{blueprint}.static" if blueprint else "static"
    if blueprint is None and filename in SHARED_FILENAMES:
        path = SHARED_STATIC_ROOT / filename
    else:
        folder = current_app.blueprints[blueprint].static_folder if blueprint else current_app.static_folder
        path = Path(folder or "") / filename
    try:
        stat = path.stat()
        version = _file_hash(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        version = __version__
    return url_for(endpoint, filename=filename, v=version)
