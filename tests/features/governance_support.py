"""Helpers shared by the governance, contributions, deletion-slowdown and temporary-accounts tests."""

from __future__ import annotations

from typing import Any

from flask import g

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope


def build_app(app_factory, *features: str):
    """A wiki with *features* switched on (plugin rows or setting toggles)."""
    app = app_factory()
    with app.test_request_context(), connection_scope():
        for feature in features:
            registry.set_enabled(feature, True)
    return app


def as_user(user: dict[str, Any] | None) -> None:
    g.user = g.real_user = user
    g.pop("_grants", None)
    g.pop("_plugin_states", None)
    g.pop("_site_settings", None)


def set_settings(app, **values: Any) -> None:
    from bananawiki.wiki import settings

    with app.test_request_context(), connection_scope():
        settings.update(values)
