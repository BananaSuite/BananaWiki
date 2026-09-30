"""BananaWiki Hosting: the portal that provisions and manages hosted wikis.

``create_app()`` builds the web application; :mod:`.runtime` is the boundary
to everything that runs tenants; :mod:`.maintenance` is the periodic service.
"""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any):
    from .app import create_app as factory

    return factory(*args, **kwargs)
