"""Helpers shared by the governance, contributions, deletion-slowdown and temporary tests.

The pages and admin features are written in parallel; when their routes or
the admin layout are not there yet, :func:`build_app` adds minimal stand-ins
so these features can be tested on their own.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, g
from jinja2 import ChoiceLoader, DictLoader

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope

_ADMIN_LAYOUT = '{% extends "base.html" %}{% block content %}{% block admin_content %}{% endblock %}{% endblock %}'


def _stub_pages() -> Blueprint:
    bp = Blueprint("pages", __name__)

    @bp.get("/")
    def home():
        return "home"

    @bp.get("/page/<slug>")
    def view(slug):
        return f"page {slug}"

    @bp.get("/page/<slug>/edit")
    def edit(slug):
        return f"edit {slug}"

    return bp


def build_app(app_factory, *features: str):
    """A wiki with *features* switched on (plugin rows or setting toggles)."""
    app = app_factory()
    if "pages" not in app.blueprints:
        app.register_blueprint(_stub_pages())
    try:
        app.jinja_env.get_template("admin/_layout.html")
    except Exception:  # noqa: BLE001 - the admin feature is not there yet
        app.jinja_env.loader = ChoiceLoader([app.jinja_env.loader, DictLoader({"admin/_layout.html": _ADMIN_LAYOUT})])
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
