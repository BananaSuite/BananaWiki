"""Profiles, avatars, contributions and the member directory.

The ``user_profiles`` row (real name, bio, birth date, avatar, published
flags) belongs to every account. Whether *other* people may open a profile
is decided by :func:`can_view_profile`: the owner and administrators always
can; everyone else needs the ``profile.view`` permission (which exists only
while the ``user_profiles`` feature is on) and a published profile that an
administrator has not disabled.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from flask import current_app, url_for
from werkzeug.datastructures import FileStorage

from ....core import passwords
from ....core.ratelimit import SqlLimiter
from ....core.timeutil import now_sql, utcnow
from ... import accounts, auth, storage
from ...config import IMAGE_EXTENSIONS, MIB
from ...db import db
from ...registry import emit
from ..pages import service as pages

AVATAR_MAX_BYTES = 1 * MIB
AVATAR_DIR = "avatars"
AVATAR_RE = re.compile(r"^avatars/[0-9a-f]{32}\.(png|jpe?g|gif|webp)$")
MAX_REAL_NAME = 100
MAX_BIO = 500
MEMBERS_PER_PAGE = 50
PASSWORD_CHECKS = 10
PASSWORD_WINDOW = 15 * 60
PROFILE_FIELDS = ("real_name", "bio", "birth_date", "avatar_filename", "page_published", "page_disabled_by_admin")


class ProfileError(ValueError):
    """A refused profile change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Passwords ─────────────────────────────────────────────────────────────────


def password_ok(user: dict[str, Any], password: str | None) -> bool:
    """Check the account password for a sensitive action, with a per-account limit.

    Raises :class:`ProfileError` once too many wrong passwords were entered.
    """
    limiter = SqlLimiter(db.session)
    key = f"user:{user['id']}"
    if limiter.exceeded(key, "account:password", PASSWORD_CHECKS, PASSWORD_WINDOW):
        raise ProfileError("auth.error.too_many_attempts")
    password = password or ""
    if len(password) <= passwords.MAX_LENGTH and passwords.verify_password(user["password"], password):
        return True
    limiter.record(key, "account:password")
    return False


# ── Profiles ──────────────────────────────────────────────────────────────────


def get_profile(user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))


def upsert_profile(user_id: str, **fields: Any) -> None:
    """Create the profile row if needed and set the given fields."""
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise KeyError(f"Not a profile field: {sorted(unknown)}")
    with db.transaction():
        db.execute("INSERT OR IGNORE INTO user_profiles (user_id, updated_at) VALUES (?, ?)", (user_id, now_sql()))
        db.update("user_profiles", {**fields, "updated_at": now_sql()}, "user_id = ?", (user_id,))


def delete_profile(user_id: str) -> None:
    """Remove the profile row and its avatar (contributions stay)."""
    profile = get_profile(user_id)
    db.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
    if profile:
        delete_upload(profile.get("avatar_filename"))


def normalize_birth_date(value: str | None) -> str:
    """``YYYY-MM-DD`` or empty; refuses impossible and future dates."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ProfileError("users.error.birth_date_invalid") from None
    if parsed.year < 1900 or parsed > utcnow().date():
        raise ProfileError("users.error.birth_date_invalid")
    return parsed.isoformat()


def is_birthday(profile: dict[str, Any] | None) -> bool:
    try:
        born = date.fromisoformat((profile or {}).get("birth_date") or "")
    except ValueError:
        return False
    today = utcnow().date()
    return (born.month, born.day) == (today.month, today.day)


def save_basics(user_id: str, *, real_name: str | None, bio: str | None, birth_date: str | None) -> None:
    upsert_profile(
        user_id,
        real_name=" ".join((real_name or "").split())[:MAX_REAL_NAME],
        bio=(bio or "").replace("\r\n", "\n").strip()[:MAX_BIO],
        birth_date=normalize_birth_date(birth_date),
    )


def can_view_profile(target: dict[str, Any], profile: dict[str, Any] | None,
                     viewer: dict[str, Any] | None) -> bool:
    if viewer is None:
        return False
    if viewer["id"] == target["id"] or auth.is_admin(viewer):
        return True
    if not auth.has_permission("profile.view", viewer):
        return False
    return bool(profile and profile.get("page_published") and not profile.get("page_disabled_by_admin"))


def can_edit_own_profile(user: dict[str, Any] | None = None) -> bool:
    return auth.has_permission("profile.edit_own", user)


# ── Uploaded images (avatars, backgrounds) ────────────────────────────────────


def upload_url(name: str | None) -> str:
    return url_for("uploaded_file", filename=name) if name else ""


def store_image(upload: FileStorage | None, subdir: str, *, max_bytes: int) -> tuple[str, Path]:
    """Store an image through :mod:`storage` and move it into ``uploads/<subdir>/``."""
    stored = storage.save(upload, "uploads", allowed=IMAGE_EXTENSIONS, max_bytes=max_bytes, images_only=True)
    source = storage.resolve("uploads", stored.filename)
    assert source is not None
    target_dir = storage.folder_path("uploads") / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored.filename
    os.replace(source, target)
    return f"{subdir}/{stored.filename}", target


def delete_upload(name: str | None) -> None:
    if name:
        storage.delete("uploads", name)


def save_avatar(user_id: str, upload: FileStorage | None) -> str:
    name, _path = store_image(upload, AVATAR_DIR, max_bytes=AVATAR_MAX_BYTES)
    previous = (get_profile(user_id) or {}).get("avatar_filename")
    upsert_profile(user_id, avatar_filename=name)
    if previous and previous != name:
        delete_upload(previous)
    return name


def remove_avatar(user_id: str) -> None:
    profile = get_profile(user_id)
    if profile and profile.get("avatar_filename"):
        upsert_profile(user_id, avatar_filename="")
        delete_upload(profile["avatar_filename"])


# ── Contributions ─────────────────────────────────────────────────────────────


def _visible(viewer: dict[str, Any] | None) -> tuple[str, list[Any]]:
    return pages.visible_filter(viewer)


def recent_contributions(user_id: str, viewer: dict[str, Any] | None, *, limit: int = 50) -> list[dict[str, Any]]:
    where, params = _visible(viewer)
    return db.all(
        "SELECT ph.id, ph.created_at, ph.edit_message, p.title AS page_title, p.slug AS page_slug "
        f"FROM page_history ph JOIN pages p ON p.id = ph.page_id WHERE ph.edited_by = ? AND {where} "
        "ORDER BY ph.id DESC LIMIT ?",
        [user_id, *params, limit],
    )


def contribution_years(user_id: str, viewer: dict[str, Any] | None) -> list[int]:
    where, params = _visible(viewer)
    rows = db.column(
        "SELECT DISTINCT substr(ph.created_at, 1, 4) FROM page_history ph JOIN pages p ON p.id = ph.page_id "
        f"WHERE ph.edited_by = ? AND {where}",
        [user_id, *params],
    )
    return sorted({int(year) for year in rows if year and year.isdigit()}, reverse=True)


def contribution_calendar(user_id: str, viewer: dict[str, Any] | None, year: int) -> dict[str, Any]:
    """Edits per day in *year* plus the visible edits behind each day (for the heatmap)."""
    where, params = _visible(viewer)
    bounds = [f"{year}-01-01", f"{year + 1}-01-01"]
    rows = db.all(
        "SELECT ph.created_at, ph.edit_message, p.title, p.slug FROM page_history ph "
        f"JOIN pages p ON p.id = ph.page_id WHERE ph.edited_by = ? AND ph.created_at >= ? AND ph.created_at < ? "
        f"AND {where} ORDER BY ph.id DESC LIMIT 2000",
        [user_id, *bounds, *params],
    )
    counts: dict[str, int] = {}
    details: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        day = row["created_at"][:10]
        counts[day] = counts.get(day, 0) + 1
        details.setdefault(day, []).append({
            "time": row["created_at"][11:16],
            "title": row["title"],
            "url": url_for("pages.view", slug=row["slug"]),
            "message": row["edit_message"] or "",
        })
    return {"year": year, "counts": counts, "details": details, "total": sum(counts.values())}


def role_history(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT r.*, u.username AS changed_by_username FROM role_history r "
        "LEFT JOIN users u ON u.id = r.changed_by WHERE r.user_id = ? ORDER BY r.changed_at DESC, r.id DESC",
        (user_id,),
    )


# ── Member directory ──────────────────────────────────────────────────────────


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def members(viewer: dict[str, Any], query: str = "", page: int = 1) -> tuple[list[dict[str, Any]], int]:
    """Administrators see every account; others see published profiles."""
    clauses: list[str] = []
    params: list[Any] = []
    if not auth.is_admin(viewer):
        clauses.append(
            "p.page_published = 1 AND p.page_disabled_by_admin = 0 AND u.suspended = 0 "
            "AND u.approval_status = 'approved'"
        )
    query = (query or "").strip()[:100]
    if query:
        clauses.append("(u.username LIKE ? ESCAPE '\\' OR p.real_name LIKE ? ESCAPE '\\')")
        params += [_like(query), _like(query)]
    where = " AND ".join(clauses) or "1"
    base = f"FROM users u LEFT JOIN user_profiles p ON p.user_id = u.id WHERE {where}"
    total = int(db.scalar(f"SELECT COUNT(*) {base}", params, default=0))
    rows = db.all(
        "SELECT u.id, u.username, u.role, u.suspended, u.approval_status, COALESCE(p.real_name, '') AS real_name, "
        "COALESCE(p.avatar_filename, '') AS avatar_filename, COALESCE(p.page_published, 0) AS page_published, "
        f"COALESCE(p.page_disabled_by_admin, 0) AS page_disabled_by_admin {base} "
        "ORDER BY u.username COLLATE NOCASE LIMIT ? OFFSET ?",
        [*params, MEMBERS_PER_PAGE, (max(page, 1) - 1) * MEMBERS_PER_PAGE],
    )
    pages_count = max(1, -(-total // MEMBERS_PER_PAGE))
    return rows, pages_count


# ── Account ──────────────────────────────────────────────────────────────────


def active_admin_count() -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner') AND suspended = 0", default=0
    ))


def delete_own_account(user: dict[str, Any], password: str | None) -> None:
    """Self-service deletion: password confirmed, never the last administrator."""
    if user.get("is_superuser"):
        raise ProfileError("users.error.protected_account")
    if not password_ok(user, password):
        raise ProfileError("auth.error.current_password_wrong")
    if user["role"] in ("admin", "owner") and not user.get("suspended") and active_admin_count() <= 1:
        raise ProfileError("users.error.last_admin")
    delete_account(user, deleted_by=user["id"])


def delete_account(user: dict[str, Any], *, deleted_by: str | None) -> None:
    """Delete an account and the images only it referenced."""
    from . import preferences

    files = [(get_profile(user["id"]) or {}).get("avatar_filename"),
             preferences.stored(user).get("background_image")]
    try:
        accounts.delete(user, deleted_by=deleted_by)
    except accounts.AccountError as error:
        raise ProfileError(error.key, **error.values) from error
    for name in files:
        delete_upload(name)


def may_become_owner(user: dict[str, Any]) -> bool:
    """Owners are appointed by owners or superusers (``admin.change_role``). An administrator
    promotes themselves only when the wiki has no owner at all, or when they are a superuser."""
    return auth.is_admin(user) and user["role"] != "owner" and (
        bool(user.get("is_superuser")) or accounts.owners_count() == 0)


def set_owner_status(user: dict[str, Any], password: str | None) -> str:
    """Toggle between admin and owner for the signed-in administrator; return the new role.

    Stepping down is always possible (except for the last owner). Stepping up is limited by
    :func:`may_become_owner`: owners outrank administrators, so an administrator who could
    make themselves owner could then change every other administrator's account.
    """
    if not auth.is_admin(user):
        raise ProfileError("error.forbidden")
    if not password_ok(user, password):
        raise ProfileError("auth.error.current_password_wrong")
    new_role = "owner" if user["role"] == "admin" else "admin"
    with db.transaction():
        if new_role == "owner" and not may_become_owner(user):
            raise ProfileError("users.error.owner_needs_owner")
        if new_role == "owner" and db.scalar("SELECT 1 FROM temp_roles WHERE user_id = ?", (user["id"],)):
            raise ProfileError("users.error.owner_blocked_by_temporary_role")
        if new_role == "admin" and accounts.owners_count() <= 1:
            raise ProfileError("users.error.last_owner")
        db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user["id"]))
        db.insert("role_history", {"user_id": user["id"], "old_role": user["role"], "new_role": new_role,
                                   "changed_by": user["id"], "changed_at": now_sql()})
    emit("user.role_changed", user=accounts.by_id(user["id"]), old_role=user["role"], new_role=new_role,
         changed_by=user["id"])
    return new_role


def may_reactivate_self(user: dict[str, Any]) -> bool:
    """Whether a suspended administrator may lift the suspension themselves (1.4 allowed any).

    Not when an owner or superuser imposed it: that suspension is exactly what the
    hierarchy exists for. Owners and superusers can always lift their own.
    """
    if not auth.is_admin(user) or not user.get("suspended"):
        return False
    if user["role"] == "owner" or user.get("is_superuser"):
        return True
    performer = db.scalar("SELECT performed_by FROM suspension_audit WHERE user_id = ? AND action = 'suspend' "
                          "ORDER BY created_at DESC, id DESC LIMIT 1", (user["id"],))
    by = accounts.by_id(performer) if performer and performer != user["id"] else None
    return not (by and (by["role"] == "owner" or by.get("is_superuser")))


def reactivate_self(user: dict[str, Any]) -> None:
    """Lift one's own suspension where :func:`may_reactivate_self` allows it."""
    if not may_reactivate_self(user):
        raise ProfileError("error.forbidden")
    with db.transaction():
        db.execute(
            "UPDATE users SET suspended = 0, suspended_until = NULL, suspend_reason = NULL, "
            "suspend_reason_visible = 0, suspend_time_visible = 0 WHERE id = ?",
            (user["id"],),
        )
        db.insert("suspension_audit", {"user_id": user["id"], "action": "unsuspend", "performed_by": user["id"],
                                       "created_at": now_sql()})


# ── Event handlers and housekeeping ───────────────────────────────────────────


def on_user_renamed(user: dict[str, Any], old_username: str, changed_by: str | None = None) -> None:
    pages.rewrite_mentions(old_username, "@" + user["username"])


def on_user_deleted(user: dict[str, Any], deleted_by: str | None = None) -> None:
    pages.rewrite_mentions(user["username"], "@account deleted")


def collect_orphan_images(min_age_seconds: int = 3600) -> int:
    """Delete avatars and backgrounds no account references (job)."""
    from . import preferences

    referenced = set(db.column("SELECT avatar_filename FROM user_profiles WHERE avatar_filename != ''"))
    for raw in db.column("SELECT accessibility FROM users WHERE accessibility LIKE '%backgrounds/%'"):
        referenced.add(preferences.parse(raw).get("background_image", ""))
    root = storage.folder_path("uploads")
    cutoff = utcnow().timestamp() - min_age_seconds
    removed = 0
    for subdir in (AVATAR_DIR, preferences.BACKGROUND_DIR):
        folder = root / subdir
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            name = f"{subdir}/{path.name}"
            if path.is_file() and name not in referenced and path.stat().st_mtime < cutoff:
                storage.delete("uploads", name)
                removed += 1
    if removed:
        current_app.logger.info("Removed %d unused profile images.", removed)
    return removed
