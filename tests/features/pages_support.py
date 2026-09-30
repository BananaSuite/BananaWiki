"""Helpers for the pages and page history tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.pages import categories, service


def in_app(app, fn: Callable[[], Any]) -> Any:
    """Run *fn* inside a request context with its own connection (as the system)."""
    with app.test_request_context(), connection_scope():
        return fn()


def make_page(app, title: str, content: str = "", *, category_id: int | None = None,
              author_id: str | None = None) -> dict[str, Any]:
    return in_app(app, lambda: service.create(title, content, category_id=category_id, author_id=author_id))


def make_category(app, name: str, parent_id: int | None = None) -> dict[str, Any]:
    return in_app(app, lambda: categories.create(name, parent_id))


def get_page(app, page_id: int) -> dict[str, Any] | None:
    return in_app(app, lambda: service.get(page_id))


def restrict(db, user: dict[str, Any], *, keys: set[str] | None = None, read: list[int] | None = None,
             write: list[int] | None = None) -> None:
    """Give *user* explicit permissions and optional category allow-lists (read/write)."""
    from bananawiki.wiki import permissions

    role = user["role"]
    chosen = permissions.defaults(role) if keys is None else permissions.sanitize(role, keys)
    db.execute("DELETE FROM user_permissions WHERE user_id = ?", (user["id"],))
    for key in chosen:
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (user["id"], key))
    for access_type, ids in (("read", read), ("write", write)):
        db.execute("INSERT OR REPLACE INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, ?)",
                   (user["id"], access_type, 1 if ids is not None else 0))
        for category_id in ids or []:
            db.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, ?)",
                       (user["id"], category_id, access_type))


def add_interceptor(app, point: str, handler: Callable[..., Any], feature_id: str = "test_probe") -> None:
    reg = app.extensions["bananawiki.registry"]
    feature = reg.features.get(feature_id)
    if feature is None:
        reg.add(registry.Feature(id=feature_id, name=feature_id, toggle="always", interceptors={point: handler}))
    else:
        feature.interceptors[point] = handler
        reg._interceptors.setdefault(point, []).append((feature_id, handler))


def set_feature(app, feature_id: str, enabled: bool) -> None:
    in_app(app, lambda: registry.set_enabled(feature_id, enabled))
