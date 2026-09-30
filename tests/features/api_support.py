"""Helpers for the REST API tests."""

from __future__ import annotations

from typing import Any

from bananawiki.wiki.features.api_service import tokens

from .pages_support import in_app, set_feature


def enable_api(app, db) -> None:
    set_feature(app, "api_service", True)
    db.execute("UPDATE site_settings SET api_service_enabled = 1 WHERE id = 1")


def issue(app, user: dict[str, Any], scopes=("pages",), *, read: bool = True, write: bool = True,
          expires_at: str | None = None, name: str = "test") -> str:
    grant = tokens.Grant(read, write, tuple(scopes))
    raw, _token_id = in_app(app, lambda: tokens.create(user["id"], name=name, grant=grant, expires_at=expires_at))
    return raw


def call(client, method: str, path: str, token: str | None = None, json: Any = None, **kwargs: Any):
    headers = kwargs.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.open("/api/v1" + path, method=method, json=json, headers=headers, **kwargs)
