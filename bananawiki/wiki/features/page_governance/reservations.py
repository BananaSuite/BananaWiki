"""Page reservations ("check-outs").

An editor reserves a page for ``page_reservation_duration_hours``; while the
reservation lasts other editors cannot edit or delete the page
(administrators can, with a warning). Releasing a page starts a per-page
cooldown of ``page_reservation_cooldown_hours`` for that editor. Each editor
may hold at most their quota of reservations at once (see :mod:`.quota`). A
page protected by someone else cannot be reserved (see :mod:`.protection`).

``page_reservations`` has one row per page (``UNIQUE(page_id)``); a row is
active while ``released_at`` is NULL, ``expires_at`` is in the future and its
holder still has an editing role and may still edit the page (one who lost
access to it, say after a move to a restricted category, neither blocks
others nor sees it listed; administrators still see and release it under
**Admin → Check-outs**). Every change runs in one ``BEGIN
IMMEDIATE`` transaction, so concurrent requests cannot both reserve a page or
exceed a quota. An administrator's release, assignment and cooldown
clearing are audited.
"""

from __future__ import annotations

from typing import Any

from ....core.timeutil import now_sql, sql_in
from ... import accounts, auth, settings
from ...db import db
from ...registry import is_enabled
from ..audit.service import record
from ..pages import service as pages
from . import protection, quota
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


def _holder_may_edit(page_id: int, holder: dict[str, Any] | None) -> bool:
    """Whether the holder of a check-out on page *page_id* may still edit it (and so read it)."""
    page = pages.get(page_id, with_content=False)
    return page is not None and holder is not None and pages.can_edit(page, holder)


def current(page_id: int) -> dict[str, Any] | None:
    """The active reservation of a page (with the holder's username), or None."""
    row = db.one(
        f"SELECT r.*, u.username FROM page_reservations r JOIN users u ON u.id = r.user_id "
        f"WHERE r.page_id = ? AND {_ACTIVE}",
        (page_id, now_sql()),
    )
    return row if row is not None and _holder_may_edit(page_id, accounts.by_id(row["user_id"])) else None


def cooldown_until(page_id: int, user_id: str) -> str | None:
    return db.scalar(
        "SELECT cooldown_until FROM user_page_cooldowns WHERE page_id = ? AND user_id = ? AND cooldown_until > ?",
        (page_id, user_id, now_sql()),
    )


def active_count(user_id: str) -> int:
    return len(of_user(user_id))


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
            and pages.can_edit(page, user) and not protection.blocks(page, user))


def _refuse_if_protected(page: dict[str, Any], user: dict[str, Any]) -> None:
    """A reservation must not lock the controller out of a page someone else protects."""
    guard = protection.state(page, user)
    if guard is not None and not guard["is_controller"]:
        raise GovernanceError("page_governance.reservation.error.protected_by", user=guard["controller_name"])


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
        _refuse_if_protected(page, user)
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


def _holder_name(page_id: int) -> str | None:
    """Username of whoever holds an unreleased, unexpired check-out of *page_id* (whatever their role)."""
    return db.scalar("SELECT u.username FROM page_reservations r JOIN users u ON u.id = r.user_id "
                     "WHERE r.page_id = ? AND r.released_at IS NULL AND r.expires_at > ?", (page_id, now_sql()))


def force_release(page_id: int) -> bool:
    """Administrator release: no cooldown for the holder (audited)."""
    with db.transaction():
        now = now_sql()
        holder = _holder_name(page_id)
        released = bool(db.execute(
            "UPDATE page_reservations SET released_at = ? WHERE page_id = ? AND released_at IS NULL "
            "AND expires_at > ?", (now, page_id, now),
        ).rowcount)
    if released:
        record("page_governance.checkout_released", target_type="page", target_id=page_id,
               details={"holder": holder})
    return released


def assign(page: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Administrator check-out on behalf of *target*, ignoring reservations, cooldowns and quotas."""
    if not active():
        raise GovernanceError("page_governance.reservation.error.disabled")
    if page.get("is_home"):
        raise GovernanceError("page_governance.reservation.error.home")
    if not pages.can_edit(page, target):
        raise GovernanceError("page_governance.reservation.error.target_cannot_edit", user=target["username"])
    with db.transaction():
        _refuse_if_protected(page, target)
        previous = _holder_name(page["id"])
        db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ? AND user_id = ?", (page["id"], target["id"]))
        row = _insert(page["id"], target["id"])
    # The check-out locks other editors out in the holder's name: the log says who gave it to them.
    record("page_governance.checkout_assigned", target_type="page", target_id=page["id"],
           details={"title": page.get("title"), "holder": target["username"], "previous_holder": previous,
                    "until": row["expires_at"]})
    return row


def clear_cooldowns(page_id: int, user_id: str | None = None) -> int:
    if user_id:
        count = db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ? AND user_id = ?",
                           (page_id, user_id)).rowcount
    else:
        count = db.execute("DELETE FROM user_page_cooldowns WHERE page_id = ?", (page_id,)).rowcount
    if count:
        record("page_governance.cooldowns_cleared", target_type="page", target_id=page_id,
               details={"count": count, **({"user_id": user_id} if user_id else {})})
    return count


def all_active() -> list[dict[str, Any]]:
    """Every unreleased check-out, for administrators (including those whose holder lost access)."""
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
    """The active check-outs of *user_id*: never one on a page they may no longer edit or read."""
    holder = accounts.by_id(user_id)
    rows = db.all(
        f"SELECT r.*, p.title, p.slug FROM page_reservations r JOIN pages p ON p.id = r.page_id "
        f"JOIN users u ON u.id = r.user_id WHERE r.user_id = ? AND {_ACTIVE} ORDER BY r.expires_at",
        (user_id, now_sql()),
    )
    return [row for row in rows if _holder_may_edit(row["page_id"], holder)]


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
        if _holder_may_edit(row["page_id"], accounts.by_id(row["user_id"])):
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
