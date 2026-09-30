"""Blueprints of the hosting portal. URLs are those of 1.4."""

from __future__ import annotations

from flask import Flask


def register(app: Flask) -> None:
    from . import account, admin, api, auth, dashboard, oauth, public

    for module in (public, auth, account, dashboard, admin, api, oauth):
        app.register_blueprint(module.bp)
