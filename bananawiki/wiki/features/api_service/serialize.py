"""JSON shapes the API returns. Timestamps are ISO 8601 in UTC (``2025-01-31T09:30:00Z``)."""

from __future__ import annotations

from typing import Any

from ....core.timeutil import parse


def iso(value: Any) -> str | None:
    moment = parse(value) if value else None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def page_summary(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": page["id"],
        "title": page["title"],
        "slug": page["slug"],
        "category_id": page.get("category_id"),
        "is_home": bool(page.get("is_home")),
        "revision": int(page.get("revision") or 0),
        "created_at": iso(page.get("created_at")),
        "last_edited_at": iso(page.get("last_edited_at")),
        "last_edited_by": page.get("last_edited_by"),
    }


def page_full(page: dict[str, Any]) -> dict[str, Any]:
    return {**page_summary(page), "content": page.get("content", ""),
            "pending_deletion": bool(page.get("pending_deletion"))}


def category(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "parent_id": row.get("parent_id"),
        "sort_order": row.get("sort_order", 0),
        "sequential_nav": bool(row.get("sequential_nav")),
    }


def user(row: dict[str, Any]) -> dict[str, Any]:
    """Account data safe to hand out (never the password hash or session data)."""
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "suspended": bool(row.get("suspended")),
        "approval_status": row.get("approval_status") or "approved",
        "api_access_enabled": bool(row.get("api_access_enabled")),
        "userbot_enabled": bool(row.get("userbot_enabled")),
        "created_at": iso(row.get("created_at")),
        "last_login_at": iso(row.get("last_login_at")),
    }


def history_entry(row: dict[str, Any], *, with_content: bool = False) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "title": row["title"],
        "edited_by": row.get("edited_by"),
        "editor": row.get("editor"),
        "edit_message": row.get("edit_message") or "",
        "is_revert": bool(row.get("is_revert")),
        "created_at": iso(row.get("created_at")),
    }
    if with_content:
        data["content"] = row.get("content", "")
    else:
        data["size"] = row.get("size")
    return data


def attachment(row: dict[str, Any]) -> dict[str, Any]:
    """A page or ticket attachment (never its stored file name)."""
    return {
        "id": row["id"],
        "name": row["original_name"],
        "size": row.get("file_size") or 0,
        "uploaded_by": row.get("uploaded_by"),
        "uploader": row.get("uploader") or row.get("uploader_name") or "",
        "uploaded_at": iso(row.get("uploaded_at")),
    }


def board(row: dict[str, Any], *, can_write: bool, is_owner: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row.get("description") or "",
        "visibility": row.get("visibility") or "public",
        "created_by": row.get("created_by"),
        "creator_username": row.get("creator_username") or "",
        "created_at": iso(row.get("created_at")),
        "archived_at": iso(row.get("archived_at")),
        "can_write": can_write,
        "is_owner": is_owner,
    }


def ticket(card: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """A ticket's card; with its row, also the description and who created it when."""
    data = {key: card[key] for key in ("id", "column_id", "title", "priority", "due_date", "color", "labels",
                                        "attachment_count", "comment_count", "checklist_total", "checklist_done")
            if key in card}
    data["assignees"] = [{"id": person["id"], "username": person["username"]} for person in card.get("assignees", [])]
    data["archived_at"] = iso(card.get("archived_at") or (row or {}).get("archived_at"))
    if row is not None:
        data.update(board_id=row.get("board_id"), description=row.get("description") or "",
                    created_by=row.get("created_by"), created_by_username=row.get("created_by_username") or "",
                    created_at=iso(row.get("created_at")))
    return data


def comment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticket_id": row.get("ticket_id"),
        "user_id": row.get("user_id"),
        "author": row.get("author_name") or "",
        "content": row.get("content") or "",
        "created_at": iso(row.get("created_at")),
        "updated_at": iso(row.get("updated_at")),
    }


def canvas(row: dict[str, Any], *, level: str | None, is_owner: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row.get("description") or "",
        "visibility": row.get("visibility") or "private",
        "is_archived": bool(row.get("is_archived")),
        "version": int(row.get("version") or 0),
        "creator_id": row.get("creator_id"),
        "creator_username": row.get("creator_username") or "",
        "created_at": iso(row.get("created_at")),
        "updated_at": iso(row.get("updated_at")),
        "can_edit": level == "edit",
        "is_owner": is_owner,
    }
