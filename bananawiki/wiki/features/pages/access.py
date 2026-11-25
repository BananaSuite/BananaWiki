"""Who may do what with pages and categories.

Visibility and plain editing live in :mod:`.service` (every caller shares
them). This module adds the finer permission keys the web interface needs:
page details, hiding, deletion, category management, and the interceptor
points other features use to block or redirect page actions.
"""

from __future__ import annotations

from typing import Any

from ... import auth, registry
from . import categories, service


def _user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    return auth.current_user() if user is None else user


def edit_blocked(page: dict[str, Any], user: dict[str, Any] | None = None) -> str | None:
    """Translation key explaining why *page* cannot be changed right now, or None.

    Pages pending deletion are frozen; enabled features (page protection,
    reservations, …) may block editing through the ``page.edit_blocked``
    interceptor.
    """
    if page.get("pending_deletion"):
        return "pages.blocked.pending_deletion"
    return registry.intercept("page.edit_blocked", page=page, user=_user(user))


def why_not_edit(page: dict[str, Any], user: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Why *user*, who can read *page*, may not edit it: a translation key and its values."""
    user = _user(user)
    category_id = page.get("category_id")
    if not auth.can_write_category(category_id, user):
        if category_id is None:
            return "pages.why.uncategorized", {}
        category = categories.get(category_id)
        return "pages.why.category", {"category": category["name"] if category else ""}
    if auth.has_permission("page.edit_own", user) and not service.is_own(page, user):
        return "pages.why.others", {}
    return "pages.why.no_editing", {}


def why_not_create(category_id: int | None, user: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Why *user* may not create a page in *category_id* (None: without a category, or anywhere)."""
    user = _user(user)
    if not auth.has_permission("page.create", user):
        return "pages.why.no_creating", {}
    if category_id is not None:
        category = categories.get(category_id)
        return "pages.why.create_category", {"category": category["name"] if category else ""}
    if not can_create_somewhere(user):
        return "pages.why.create_nowhere", {}
    return "pages.why.create_uncategorized", {}


def can_edit_metadata(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Change the title, address or category of *page*: its creator, or with ``page.edit_metadata``."""
    user = _user(user)
    if not service.can_edit(page, user):
        return False
    return auth.has_permission("page.edit_metadata", user) or service.is_own(page, user)


def may_write_pages(user: dict[str, Any] | None = None) -> bool:
    """Whether *user* may create or edit any page at all (the editor's helpers: previews, images)."""
    user = _user(user)
    if not user:
        return False
    return any(auth.has_permission(key, user) for key in ("page.create", "page.edit_own", "page.edit_all"))


def can_deindex(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return not page.get("is_home") and service.can_edit(page, user) and auth.has_permission("page.deindex", user)


def can_set_home(user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return bool(user) and auth.is_admin(user)


def can_create_somewhere(user: dict[str, Any] | None = None) -> bool:
    """Whether *user* may create pages in at least one place (shows the "new page" links)."""
    user = _user(user)
    if not user:
        return False
    if auth.is_admin(user):
        return True
    return auth.has_permission("page.create", user) and bool(
        writable_category_ids(user) or auth.can_write_category(None, user)
    )


def writable_category_ids(user: dict[str, Any] | None = None) -> list[int]:
    user = _user(user)
    return [c["id"] for c in categories.all_categories() if auth.can_write_category(c["id"], user)]


def category_choices(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Categories *user* may put pages into, as ``{id, path}`` sorted by path."""
    user = _user(user)
    names = categories.paths(lambda category_id: auth.can_read_category(category_id, user))
    allowed = set(writable_category_ids(user))
    return sorted(({"id": cid, "path": path} for cid, path in names.items() if cid in allowed),
                  key=lambda item: item["path"].casefold())


def can_manage_category(permission: str, category_id: int | None, user: dict[str, Any] | None = None) -> bool:
    """*permission* (``category.*``) plus write access to *category_id*.

    ``None`` stands for the top level: only writers without category
    restrictions may add or move categories there.
    """
    user = _user(user)
    if not user or not auth.has_role("editor", user):
        return False
    return auth.has_permission(permission, user) and auth.can_write_category(category_id, user)


def can_search(user: dict[str, Any] | None = None) -> bool:
    """Signed-in users need ``search.pages``; public-mode visitors may search."""
    user = _user(user)
    return user is None or auth.has_permission("search.pages", user)
