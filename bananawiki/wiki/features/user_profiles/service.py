"""Custom profile fields, custom tags and profile publication."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ....core.timeutil import now_sql
from ... import auth
from ...db import db
from ..pages.service import slugify
from ..users import service as users

FIELD_TYPES = ("text", "longtext", "number", "url", "date")
MAX_LABEL = 80
MAX_VALUE = {"text": 500, "longtext": 2000, "number": 50, "url": 500, "date": 10}
MAX_TAG_LABEL = 50
MAX_TAGS = 20
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ProfileFieldError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Moderation rules ──────────────────────────────────────────────────────────


def can_moderate(target: dict[str, Any], actor: dict[str, Any] | None = None) -> bool:
    """Administrators moderate profiles, except protected and owner accounts of others."""
    actor = auth.current_user() if actor is None else actor
    if not actor or not auth.is_admin(actor):
        return False
    if actor["id"] == target["id"]:
        return True
    return not target.get("is_superuser") and target["role"] != "owner"


# ── Field definitions ─────────────────────────────────────────────────────────


def definitions() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM user_profile_fields__definitions ORDER BY sort_order, id")


def get_definition(field_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM user_profile_fields__definitions WHERE id = ?", (field_id,))


def _clean_label(label: str | None) -> str:
    label = " ".join((label or "").split())[:MAX_LABEL]
    if not label:
        raise ProfileFieldError("profiles.error.label_required")
    return label


def _clean_type(field_type: str | None) -> str:
    if field_type not in FIELD_TYPES:
        raise ProfileFieldError("profiles.error.type_invalid")
    return field_type


def create_definition(label: str | None, field_type: str | None) -> int:
    label = _clean_label(label)
    field_type = _clean_type(field_type)
    base = slugify(label).replace("-", "_")[:40] or "field"
    with db.transaction():
        key, n = base, 2
        while db.scalar("SELECT 1 FROM user_profile_fields__definitions WHERE key = ?", (key,)):
            key, n = f"{base}_{n}", n + 1
        order = int(db.scalar("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM user_profile_fields__definitions",
                              default=0))
        return db.insert("user_profile_fields__definitions", {
            "key": key, "label": label, "field_type": field_type, "sort_order": order, "created_at": now_sql(),
        })


def update_definition(field_id: int, label: str | None, field_type: str | None) -> None:
    db.update("user_profile_fields__definitions", {"label": _clean_label(label), "field_type": _clean_type(field_type)},
              "id = ?", (field_id,))


def delete_definition(field_id: int) -> None:
    db.execute("DELETE FROM user_profile_fields__definitions WHERE id = ?", (field_id,))


def move_definition(field_id: int, offset: int) -> None:
    with db.transaction():
        ids = [row["id"] for row in definitions()]
        if field_id not in ids:
            return
        index = ids.index(field_id)
        other = index + offset
        if 0 <= other < len(ids):
            ids[index], ids[other] = ids[other], ids[index]
        db.executemany("UPDATE user_profile_fields__definitions SET sort_order = ? WHERE id = ?",
                       [(position, row_id) for position, row_id in enumerate(ids)])


# ── Values ────────────────────────────────────────────────────────────────────


def values(user_id: str) -> dict[int, dict[str, Any]]:
    rows = db.all("SELECT field_id, value, visible FROM user_profile_fields__values WHERE user_id = ?", (user_id,))
    return {row["field_id"]: row for row in rows}


def link_target(value: str | None) -> str | None:
    """*value* if it is an http(s) URL that may be rendered as a link, else None."""
    try:
        parts = urlsplit((value or "").strip())
    except ValueError:
        return None
    return value.strip() if parts.scheme in ("http", "https") and parts.netloc else None  # type: ignore[union-attr]


def shown_values(user_id: str, *, include_hidden: bool) -> list[dict[str, Any]]:
    """Field values for the profile; ``href`` is set only for URL fields holding an http(s) URL.

    Values are checked again here, not only when saved: a value typed into a
    text field whose type an administrator later changed to URL, or one
    imported from 1.4, could otherwise become a ``javascript:`` link.
    """
    visible = "" if include_hidden else " AND v.visible = 1"
    rows = db.all(
        "SELECT d.label, d.field_type, v.value, v.visible FROM user_profile_fields__values v "
        "JOIN user_profile_fields__definitions d ON d.id = v.field_id "
        f"WHERE v.user_id = ? AND v.value != ''{visible} ORDER BY d.sort_order, d.id",
        (user_id,),
    )
    return [{**row, "href": link_target(row["value"]) if row["field_type"] == "url" else None} for row in rows]


def clean_value(field_type: str, raw: str | None) -> str:
    value = (raw or "").replace("\r\n", "\n").strip()
    if field_type != "longtext":
        value = " ".join(value.split())
    if not value:
        return ""
    if len(value) > MAX_VALUE.get(field_type, 500):
        raise ProfileFieldError("profiles.error.value_too_long")
    if field_type == "number":
        try:
            float(value)
        except ValueError:
            raise ProfileFieldError("profiles.error.number_invalid") from None
    elif field_type == "url":
        if link_target(value) is None:
            raise ProfileFieldError("profiles.error.url_invalid")
    elif field_type == "date":
        try:
            value = date.fromisoformat(value).isoformat()
        except ValueError:
            raise ProfileFieldError("profiles.error.date_invalid") from None
    return value


def save_values(user_id: str, form: Any) -> None:
    """Save every defined field from a form (``value_<id>``, ``visible_<id>``)."""
    rows = []
    for field in definitions():
        value = clean_value(field["field_type"], form.get(f"value_{field['id']}"))
        rows.append((user_id, field["id"], value, 1 if form.get(f"visible_{field['id']}") else 0, now_sql()))
    with db.transaction():
        db.executemany(
            "INSERT INTO user_profile_fields__values (user_id, field_id, value, visible, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, field_id) DO UPDATE SET value = excluded.value, "
            "visible = excluded.visible, updated_at = excluded.updated_at",
            rows,
        )


# ── Custom tags ───────────────────────────────────────────────────────────────


def tags(user_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM user_custom_tags WHERE user_id = ? ORDER BY sort_order, id", (user_id,))


def _clean_tag(label: str | None, color: str | None) -> tuple[str, str]:
    label = " ".join((label or "").split())[:MAX_TAG_LABEL]
    if not label:
        raise ProfileFieldError("profiles.error.tag_label_required")
    color = (color or "").strip()
    if not HEX_COLOR.fullmatch(color):
        raise ProfileFieldError("profiles.error.tag_color_invalid")
    return label, color.lower()


def add_tag(user_id: str, label: str | None, color: str | None) -> None:
    label, color = _clean_tag(label, color)
    with db.transaction():
        count = int(db.scalar("SELECT COUNT(*) FROM user_custom_tags WHERE user_id = ?", (user_id,), default=0))
        if count >= MAX_TAGS:
            raise ProfileFieldError("profiles.error.too_many_tags", limit=MAX_TAGS)
        db.insert("user_custom_tags", {"user_id": user_id, "label": label, "color": color, "sort_order": count})


def update_tag(user_id: str, tag_id: int, label: str | None, color: str | None) -> None:
    label, color = _clean_tag(label, color)
    if not db.update("user_custom_tags", {"label": label, "color": color}, "id = ? AND user_id = ?",
                     (tag_id, user_id)):
        raise ProfileFieldError("profiles.error.tag_missing")


def delete_tag(user_id: str, tag_id: int) -> None:
    if not db.execute("DELETE FROM user_custom_tags WHERE id = ? AND user_id = ?", (tag_id, user_id)).rowcount:
        raise ProfileFieldError("profiles.error.tag_missing")


def move_tag(user_id: str, tag_id: int, offset: int) -> None:
    with db.transaction():
        ids = [row["id"] for row in tags(user_id)]
        if tag_id not in ids:
            raise ProfileFieldError("profiles.error.tag_missing")
        index = ids.index(tag_id)
        other = index + offset
        if 0 <= other < len(ids):
            ids[index], ids[other] = ids[other], ids[index]
        db.executemany("UPDATE user_custom_tags SET sort_order = ? WHERE id = ?",
                       [(position, row_id) for position, row_id in enumerate(ids)])


# ── Publication ───────────────────────────────────────────────────────────────


def set_published(user_id: str, published: bool) -> None:
    """The owner shows or hides their profile; an administrator's block wins."""
    profile = users.get_profile(user_id) or {}
    if published and profile.get("page_disabled_by_admin"):
        raise ProfileFieldError("profiles.error.disabled_by_admin")
    users.upsert_profile(user_id, page_published=1 if published else 0)


def moderate(user_id: str, action: str) -> None:
    """Administrator actions on someone's profile page."""
    if action == "disable":
        users.upsert_profile(user_id, page_disabled_by_admin=1, page_published=0)
    elif action == "enable":
        users.upsert_profile(user_id, page_disabled_by_admin=0)
    elif action == "publish":
        users.upsert_profile(user_id, page_published=1, page_disabled_by_admin=0)
    elif action == "unpublish":
        users.upsert_profile(user_id, page_published=0)
    elif action == "delete":
        users.delete_profile(user_id)
    else:
        raise ProfileFieldError("profiles.error.unknown_action")
