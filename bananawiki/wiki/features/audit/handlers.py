"""Audit entries for the events other features emit (see ARCHITECTURE.md)."""

from __future__ import annotations

from typing import Any

from .service import record


def _user_target(user: dict[str, Any] | None) -> dict[str, Any]:
    user = user or {}
    return {"target_type": "user", "target_id": user.get("id")}


def user_created(user: dict[str, Any], **_: Any) -> None:
    record("user.created", details={"username": user.get("username"), "role": user.get("role")},
           **_user_target(user))


def user_deleted(user: dict[str, Any], deleted_by: Any = None, **_: Any) -> None:
    record("user.deleted", actor_id=deleted_by, details={"username": user.get("username")}, **_user_target(user))


def user_renamed(user: dict[str, Any], old_username: str = "", changed_by: Any = None, **_: Any) -> None:
    record("user.renamed", actor_id=changed_by,
           details={"old_username": old_username, "new_username": user.get("username")}, **_user_target(user))


def user_login(user: dict[str, Any], **_: Any) -> None:
    record("user.login", actor_id=user.get("id"), details={"username": user.get("username")}, **_user_target(user))


def user_role_changed(user: dict[str, Any], old_role: str = "", new_role: str = "", changed_by: Any = None,
                      **_: Any) -> None:
    record("user.role_changed", actor_id=changed_by,
           details={"username": user.get("username"), "old_role": old_role, "new_role": new_role},
           **_user_target(user))


def user_suspended(user: dict[str, Any], until: Any = None, actor_id: Any = None, **_: Any) -> None:
    record("user.suspended", actor_id=actor_id, details={"username": user.get("username"), "until": until},
           **_user_target(user))


def user_password_reset(user: dict[str, Any], actor_id: Any = None, **_: Any) -> None:
    record("user.password_reset", actor_id=actor_id, details={"username": user.get("username")},
           **_user_target(user))


def user_password_changed(user_id: Any = None, **_: Any) -> None:
    record("user.password_changed", target_type="user", target_id=user_id)


def _page_details(page: dict[str, Any]) -> dict[str, Any]:
    return {"title": page.get("title"), "slug": page.get("slug")}


def page_deleted(page: dict[str, Any], actor_id: Any = None, **_: Any) -> None:
    record("page.deleted", actor_id=actor_id, target_type="page", target_id=page.get("id"),
           details=_page_details(page))


def page_restored(page: dict[str, Any], actor_id: Any = None, **_: Any) -> None:
    record("page.restored", actor_id=actor_id, target_type="page", target_id=page.get("id"),
           details=_page_details(page))


def category_deleted(category: dict[str, Any], actor_id: Any = None, **_: Any) -> None:
    record("category.deleted", actor_id=actor_id, target_type="category", target_id=category.get("id"),
           details={"name": category.get("name")})


EVENTS = {
    "user.created": [user_created],
    "user.deleted": [user_deleted],
    "user.renamed": [user_renamed],
    "user.login": [user_login],
    "user.role_changed": [user_role_changed],
    "user.suspended": [user_suspended],
    "user.password_reset": [user_password_reset],
    "user.password_changed": [user_password_changed],
    "page.deleted": [page_deleted],
    "page.restored": [page_restored],
    "category.deleted": [category_deleted],
}
