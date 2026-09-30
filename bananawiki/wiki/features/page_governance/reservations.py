"""Page reservations ("check-outs").

An editor reserves a page for ``page_reservation_duration_hours``; while the
reservation lasts other editors cannot edit or delete the page
(administrators can, with a warning). Releasing a page starts a per-page
cooldown of ``page_reservation_cooldown_hours`` for that editor. Each editor
may hold at most their quota of reservations at once (see :mod:`.quota`).

``page_reservations`` has one row per page (``UNIQUE(page_id)``); a row is
active while ``released_at`` is NULL, ``expires_at`` is in the future and its
holder still has an editing role. Every change runs in one ``BEGIN
IMMEDIATE`` transaction, so concurrent requests cannot both reserve a page or
exceed a quota.
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql, sql_in
from ... import auth, settings
from ...db import db
from ...registry import is_enabled
from ..pages import service as pages
from . import quota
from .errors import GovernanceError

HOLDER_ROLES = "('editor', 'admin', 'owner')"
MAX_HOURS = 24 * 365


def active() -> bool:
    return is_enabled("page_governance") and bool(settings.get("page_reservations_enabled"))


def _hours(name: str, default: int, minimum: int) -> int:
    try:
        return min(MAX_HOURS, max(minimum, int(settings.get(name, default))))
    except (TypeError, ValueError):
        return default


def duration_hours() -> int:
    return _hours("page_reservation_duration_hours", 48, 1)


def cooldown_hours() -> int:
    return _hours("page_reservation_cooldown_hours", 24, 0)


_ACTIVE = f"r.released_at IS NULL AND r.expires_at > ? AND u.role IN {HOLDER_ROLES}"


def current(page_id: int) -> dict[str, Any] | None:
    """The active reservation of a page (with the holder's username), or None."""
    return db.one(
        f"SELECT r.*, u.username FROM page_reservations r JOIN users u ON u.id = r.user_id "
        f"WHERE r.page_id = ? AND {_ACTIVE}",
        (page_id, now_sql()),
    )


def cooldown_until(page_id: int, user_id: str) -> str | None:
    return db.scalar(
        "SELECT cooldown_until FROM user_page_cooldowns WHERE page_id = ? AND user_id = ? AND cooldown_until > ?",
        (page_id, user_id, now_sql()),
    )


def active_count(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM page_reservations WHERE user_id = ? AND released_at IS NULL AND expires_at > ?",
        (user_id, now_sql()), default=0,
    ))


def status(page: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reservation state of *page* for *user*; None while reservations are off."""
    if not active() or page.get("is_home"):
        return None
    reservation = current(page["id"])
    mine = bool(reservation and user and reservation["user_id"] == user["id"])
    by_other = bool(reservation) and not mine
    admin = auth.is_admin(user) if user else False
    return {
        "reservation": reservation,
        "mine": mine,
        "by_other": by_other,
        "blocks_edit": by_other and not admin,
        "admin_override": by_other and admin,
        "cooldown_until": cooldown_until(page["id"], user["id"]) if user else None,
    }


def blocks(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    state = status(page, user)
    return bool(state and state["blocks_edit"])


def can_reserve(page: dict[str, Any], user: dict[str, Any] | None) -> bool:
    return (bool(user) and active() and not page.get("is_home") and not page.get("pending_deletion")
            and pages.can_edit(page, user))


def reserve(page: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not active():
        raise GovernanceError("page_governance.reservation.error.disabled")
    if page.get("is_home"):
        raise GovernanceError("page_governance.reservation.error.home")
    if page.get("pending_deletion"):
        raise GovernanceError("page_governance.reservation.error.pending_deletion")
    if not pages.can_edit(page, user):
        raise GovernanceError("page_governance.error.cannot_edit")
    with db.transaction():
        existing = current(page["id"])
        if existing is not None:
            if existing["user_id"] == user["id"]:
                raise GovernanceError("page_governance.reservation.error.already_yours")
            raise GovernanceError("page_governance.reservation.error.reserved_by", user=existing["username"])
        until = cooldown_until(page["id"], user["id"])
        if until:
            raise GovernanceError("page_governance.reservation.error.cooldown", until=until)
        limit = quota.effective(user["id"])
        if limit != quota.UNLIMITED and active_count(user["id"]) >= limit:
            raise GovernanceError("page_governance.reservation.error.quota", quota=limit)
        return _insert(page["id"], user["id"])


def _insert(page_id: int, user_id: str) -> dict[str, Any]:
    db.execute("DELETE FROM page_reservations WHERE page_id = ?", (page_id,))
    reservation_id = db.insert("page_reservations", {
        "page_id": page_id, "user_id": user_id, "reserved_at": now_sql(),
        "expires_at": sql_in(hours=duration_hours()),
    })
    row = db.one("SELECT * FROM page_reservations WHERE id = ?", (reservation_id,))
    assert row is not None
    return row


def release(page: dict[str, Any], user: dict[str, Any]) -> bool:
    """The holder releases the page; this starts their cooldown on it."""
    with db.transaction():
        now = now_sql()
        released = db.execute(
            "UPDATE page_reservations SET released_at = ? "
            "WHERE page_id = ? AND user_id = ? AND released_at IS NULL AND expires_at > ?",
            (now, page["id"], user["id"], now),
        ).rowcount
        if released and cooldown_hours() > 0:
            db.execute(
                "INSERT INTO user_page_cooldowns (page_id, user_id, cooldown_until) VALUES (?, ?, ?) "
                "ON CONFLICT(page_id, user_id) DO UPDATE SET cooldown_until = excluded.cooldown_until",
                (page["id"], user["id"], sql_in(hours=cooldown_hours())),
            )
    return bool(released)


def force_release(page_id: int) -> bool:
    """Administrator release: no cooldown for the holder."""
    now = now_sql()
    return bool(db.execute(
        "UPDATE page_reservations SET released_at = ? WHERE page_id = ? AND released_at IS NULL AND expires_at > ?",
        (now, page_id, now),
    ).rowcount)


def assign(page: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Administrator check-out on behalf of *target*, ignoring reservations, cooldowns and quotas."""
    if not active():
        raise GovernanceError("page_governance.reservation.error.disabled")
    if page.get("is_home"):
        raise GovernanceError("page_governance.reservation.error.home")
    if not pages.can_edit(page, target):
        raise GovernanceError("page_governance.reservation.error.target_cannot_edit", user=target["username"])
    with db.transaction():
        db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ? AND user_id = ?", (page["id"], target["id"]))
        return _insert(page["id"], target["id"])


def clear_cooldowns(page_id: int, user_id: str | None = None) -> int:
    if user_id:
        return db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ? AND user_id = ?",
                          (page_id, user_id)).rowcount
    return db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ?", (page_id,)).rowcount


def all_active() -> list[dict[str, Any]]:
    return db.all(
        f"SELECT r.*, u.username, p.title, p.slug FROM page_reservations r "
        f"JOIN users u ON u.id = r.user_id JOIN pages p ON p.id = r.page_id "
        f"WHERE {_ACTIVE} ORDER BY r.expires_at",
        (now_sql(),),
    )


def all_cooldowns() -> list[dict[str, Any]]:
    return db.all(
        "SELECT c.page_id, c.user_id, c.cooldown_until, u.username, p.title, p.slug FROM user_page_cooldowns c "
        "JOIN users u ON u.id = c.user_id JOIN pages p ON p.id = c.page_id "
        "WHERE c.cooldown_until > ? ORDER BY c.cooldown_until",
        (now_sql(),),
    )


def of_user(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT r.*, p.title, p.slug FROM page_reservations r JOIN pages p ON p.id = r.page_id "
        "WHERE r.user_id = ? AND r.released_at IS NULL AND r.expires_at > ? ORDER BY r.expires_at",
        (user_id, now_sql()),
    )


def statuses(page_ids: list[int], user_id: str) -> dict[int, dict[str, Any]]:
    """Reservation and cooldown state for many pages in two queries."""
    result: dict[int, dict[str, Any]] = {pid: {"reservation": None, "cooldown_until": None} for pid in page_ids}
    if not page_ids:
        return result
    marks = ",".join("?" for _ in page_ids)
    now = now_sql()
    for row in db.all(
        f"SELECT r.page_id, r.user_id, r.expires_at, u.username FROM page_reservations r "
        f"JOIN users u ON u.id = r.user_id WHERE r.page_id IN ({marks}) AND {_ACTIVE}",
        (*page_ids, now),
    ):
        result[row["page_id"]]["reservation"] = row
    for row in db.all(
        f"SELECT page_id, cooldown_until FROM user_page_cooldowns "
        f"WHERE user_id = ? AND page_id IN ({marks}) AND cooldown_until > ?",
        (user_id, *page_ids, now),
    ):
        result[row["page_id"]]["cooldown_until"] = row["cooldown_until"]
    return result


def cleanup(*, released_retention_days: int = 30, request_retention_days: int = 90) -> None:
    """Close expired reservations and drop stale rows (idempotent)."""
    now = now_sql()
    with db.transaction():
        db.execute(
            "UPDATE page_reservations SET released_at = expires_at WHERE released_at IS NULL AND expires_at <= ?",
            (now,),
        )
        db.execute("DELETE FROM user_page_cooldowns WHERE cooldown_until <= ?", (now,))
        db.execute("DELETE FROM page_reservations WHERE released_at IS NOT NULL AND released_at < ?",
                   (sql_in(days=-released_retention_days),))
        db.execute(
            "DELETE FROM reservation_quota_requests WHERE status IN ('approved', 'denied', 'cancelled') "
            "AND created_at < ?",
            (sql_in(days=-request_retention_days),),
        )
