"""Portal-wide announcement banners (``hosting_banners``)."""

from __future__ import annotations

import re
from typing import Any

from ..core.timeutil import now_sql
from .db import db
from .errors import ServiceError

COLORS = ("red", "orange", "yellow", "blue", "green")
VISIBILITIES = ("both", "logged_in", "logged_out")
AUDIENCES = ("all", "allowlist", "denylist")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate(values: dict[str, Any], audience_ids: list[str]) -> tuple[dict[str, Any], list[str]]:
    content = (values.get("content") or "").strip()
    if not content or len(content) > 2000:
        raise ServiceError("hosting.banners.invalid_content")
    if (values.get("color") not in COLORS or values.get("visibility") not in VISIBILITIES
            or values.get("audience_mode") not in AUDIENCES):
        raise ServiceError("hosting.banners.invalid_options")
    ids = list(dict.fromkeys(audience_ids))
    if values["audience_mode"] == "all":
        ids = []
    elif not ids:
        raise ServiceError("hosting.banners.audience_required")
    if ids:
        marks = ", ".join("?" for _ in ids)
        known = db.scalar(f"SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL AND id IN ({marks})", ids, default=0)
        if known != len(ids):
            raise ServiceError("hosting.banners.invalid_audience")
    background, text = values.get("custom_background") or None, values.get("custom_text_color") or None
    if (background or text) and not (background and text and _HEX.match(background) and _HEX.match(text)):
        raise ServiceError("hosting.banners.invalid_colors")
    clean = {
        "content": content, "color": values["color"], "visibility": values["visibility"],
        "audience_mode": values["audience_mode"], "expires_at": values.get("expires_at"),
        "not_removable": 1 if values.get("not_removable") else 0, "show_countdown": 1 if values.get("show_countdown") else 0,
        "custom_background": background, "custom_text_color": text,
    }
    return clean, ids


def _set_audience(banner_id: int, ids: list[str]) -> None:
    db.execute("DELETE FROM hosting_banner_audience WHERE banner_id = ?", (banner_id,))
    db.executemany("INSERT INTO hosting_banner_audience (banner_id, account_id) VALUES (?, ?)",
                   [(banner_id, account_id) for account_id in ids])


def create(values: dict[str, Any], audience_ids: list[str], created_by: str) -> int:
    clean, ids = validate(values, audience_ids)
    with db.transaction():
        banner_id = db.insert("hosting_banners", {**clean, "created_by": created_by, "created_at": now_sql(),
                                                  "updated_at": now_sql()})
        _set_audience(banner_id, ids)
    return banner_id


def update(banner_id: int, values: dict[str, Any], audience_ids: list[str], active: bool) -> None:
    clean, ids = validate(values, audience_ids)
    with db.transaction():
        db.update("hosting_banners", {**clean, "is_active": 1 if active else 0, "updated_at": now_sql()},
                  "id = ?", (banner_id,))
        db.execute("UPDATE hosting_banners SET revision = revision + 1 WHERE id = ?", (banner_id,))
        _set_audience(banner_id, ids)


def delete(banner_id: int) -> bool:
    return db.execute("DELETE FROM hosting_banners WHERE id = ?", (banner_id,)).rowcount > 0


def get(banner_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM hosting_banners WHERE id = ?", (banner_id,))


def list_all() -> list[dict[str, Any]]:
    rows = db.all("SELECT b.*, a.username AS creator_name FROM hosting_banners b "
                  "LEFT JOIN accounts a ON a.id = b.created_by ORDER BY b.created_at DESC")
    for row in rows:
        row["audience_ids"] = db.column("SELECT account_id FROM hosting_banner_audience WHERE banner_id = ?",
                                        (row["id"],))
    return rows


def active_for(account_id: str | None) -> list[dict[str, Any]]:
    signed_in = 1 if account_id else 0
    return db.all(
        "SELECT * FROM hosting_banners b WHERE is_active = 1 AND (expires_at IS NULL OR expires_at > ?) "
        "AND (visibility = 'both' OR (visibility = 'logged_in' AND ? = 1) OR (visibility = 'logged_out' AND ? = 0)) "
        "AND (audience_mode = 'all' "
        " OR (audience_mode = 'allowlist' AND EXISTS (SELECT 1 FROM hosting_banner_audience h "
        "     WHERE h.banner_id = b.id AND h.account_id = ?)) "
        " OR (audience_mode = 'denylist' AND NOT EXISTS (SELECT 1 FROM hosting_banner_audience h "
        "     WHERE h.banner_id = b.id AND h.account_id = ?))) ORDER BY created_at DESC",
        (now_sql(), signed_in, signed_in, account_id or "", account_id or ""),
    )
