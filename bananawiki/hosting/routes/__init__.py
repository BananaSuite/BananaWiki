"""Blueprints of the hosting portal. URLs are those of 1.4."""

from __future__ import annotations

from flask import Flask


def register(app: Flask) -> None:
    from . import account, admin, admin_links, api, auth, dashboard, oauth, public  # noqa: F401 - admin_links adds views to admin

    for module in (public, auth, account, dashboard, admin, api, oauth):
        app.register_blueprint(module.bp)
