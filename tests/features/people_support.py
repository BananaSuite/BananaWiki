"""Helpers for the users, profiles, badges, data export and leaderboard tests."""

from __future__ import annotations

import io
from typing import Any

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope


def in_app(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


def set_feature(app, feature_id: str, enabled: bool) -> None:
    in_app(app, lambda: registry.set_enabled(feature_id, enabled))


def make_page(app, title: str, content: str = "", *, author_id: str | None = None,
              category_id: int | None = None) -> dict[str, Any]:
    from bananawiki.wiki.features.pages import service

    return in_app(app, lambda: service.create(title, content, author_id=author_id, category_id=category_id))


def edit_page(app, page_id: int, content: str, *, author_id: str | None) -> dict[str, Any]:
    from bananawiki.wiki.features.pages import service

    return in_app(app, lambda: service.update(service.get(page_id), content=content, author_id=author_id))


def make_category(app, name: str) -> dict[str, Any]:
    from bananawiki.wiki.features.pages import categories

    return in_app(app, lambda: categories.create(name))


def restrict_read(db, user: dict[str, Any], category_ids: list[int]) -> None:
    """Limit a plain user to reading *category_ids* (keeping the role defaults)."""
    from bananawiki.wiki import permissions

    for key in permissions.defaults(user["role"]):
        db.execute("INSERT OR IGNORE INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (user["id"], key))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
               (user["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 0)",
               (user["id"],))
    for category_id in category_ids:
        db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'read')",
                   (user["id"], category_id))


def publish(db, user: dict[str, Any], published: bool = True) -> None:
    db.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES (?)", (user["id"],))
    db.execute("UPDATE user_profiles SET page_published = ? WHERE user_id = ?", (1 if published else 0, user["id"]))


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (40, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


