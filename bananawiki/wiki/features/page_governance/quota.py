"""Reservation quotas and the requests users file to raise them.

A user's quota is ``users.reserved_pages_quota`` (``-1`` = unlimited,
``NULL`` = the site default ``default_reserved_pages_quota``). Users ask for
more with a request; requests up to ``reservation_quota_auto_approve_max``
are approved on the spot. After any decision or cancellation the user waits
``quota_request_cooldown_hours`` before filing the next request (the same
per-user timestamp the contribution quota uses, as in 1.4).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ....core.timeutil import now_sql, parse, sql_in, utcnow
from ... import attention, settings
from ...db import db
from .errors import GovernanceError

MAX_REASON = 2000
MAX_REVIEW_REASON = 1000
MAX_QUOTA = 100_000
UNLIMITED = -1


def _int_setting(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(settings.get(name, default)))
    except (TypeError, ValueError):
        return default


def default_quota() -> int:
    return _int_setting("default_reserved_pages_quota", 5, 1)


def normalize(value: Any) -> int:
    """``-1`` (unlimited) or a quota between 1 and :data:`MAX_QUOTA`."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise GovernanceError("page_governance.quota.error.invalid") from None
    if number == UNLIMITED:
        return UNLIMITED
    if number < 1 or number > MAX_QUOTA:
        raise GovernanceError("page_governance.quota.error.invalid")
    return number


def effective(user_id: str) -> int:
    stored = db.scalar("SELECT reserved_pages_quota FROM users WHERE id = ?", (user_id,))
    if stored is None:
        return default_quota()
    try:
        return normalize(stored)
    except GovernanceError:
        return default_quota()


def set_quota(user_id: str, quota: int | None) -> None:
    """Set a user's quota directly (``None`` returns to the site default)."""
    value = None if quota is None else normalize(quota)
    db.execute("UPDATE users SET reserved_pages_quota = ? WHERE id = ?", (value, user_id))


def _cooldown_hours() -> int:
    return _int_setting("quota_request_cooldown_hours", 0, 0)


def _start_cooldown(user_id: str) -> None:
    hours = _cooldown_hours()
    if hours > 0:
        db.execute("UPDATE users SET quota_request_cooldown_until = ? WHERE id = ?",
                   (sql_in(hours=hours), user_id))


_SELECT = (
    "SELECT r.*, u.username, reviewer.username AS reviewed_by_username FROM reservation_quota_requests r "
    "JOIN users u ON u.id = r.user_id LEFT JOIN users reviewer ON reviewer.id = r.reviewed_by"
)


def get_request(request_id: int) -> dict[str, Any] | None:
    return db.one(f"{_SELECT} WHERE r.id = ?", (request_id,))


def pending_request(user_id: str) -> dict[str, Any] | None:
    return db.one(f"{_SELECT} WHERE r.user_id = ? AND r.status = 'pending' ORDER BY r.id DESC LIMIT 1", (user_id,))


def history(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return db.all(f"{_SELECT} WHERE r.user_id = ? ORDER BY r.id DESC LIMIT ?", (user_id, limit))


def all_pending() -> list[dict[str, Any]]:
    return db.all(f"{_SELECT} WHERE r.status = 'pending' ORDER BY r.id")


def oldest_pending() -> str | None:
    return db.scalar("SELECT MIN(created_at) FROM reservation_quota_requests WHERE status = 'pending'")


def count_pending() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM reservation_quota_requests WHERE status = 'pending'", default=0))


def cooldown_remaining_minutes(user_id: str) -> int:
    if _cooldown_hours() <= 0:
        return 0
    until = parse(db.scalar("SELECT quota_request_cooldown_until FROM users WHERE id = ?", (user_id,)))
    if until is None or until <= utcnow():
        return 0
    return max(1, int((until - utcnow()).total_seconds() // 60))


def create_request(user_id: str, requested: Any, reason: str) -> dict[str, Any]:
    """File a request; it may be approved automatically. Returns the stored row."""
    quota = normalize(requested)
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError("page_governance.quota.error.reason_required")
    if len(reason) > MAX_REASON:
        raise GovernanceError("page_governance.quota.error.reason_too_long", limit=MAX_REASON)
    auto_max = _int_setting("reservation_quota_auto_approve_max", 0, 0)
    try:
        with db.transaction():
            if db.scalar("SELECT 1 FROM reservation_quota_requests WHERE user_id = ? AND status = 'pending'",
                         (user_id,)):
                raise GovernanceError("page_governance.quota.error.already_pending")
            minutes = cooldown_remaining_minutes(user_id)
            if minutes:
                raise GovernanceError("page_governance.quota.error.cooldown", minutes=minutes)
            current = effective(user_id)
            automatic = quota != UNLIMITED and current != UNLIMITED and current < quota <= auto_max
            now = now_sql()
            request_id = db.insert("reservation_quota_requests", {
                "user_id": user_id, "requested_quota": quota, "reason": reason,
                "status": "approved" if automatic else "pending",
                "review_source": "automatic" if automatic else "manual",
                "reviewed_at": now if automatic else None, "created_at": now,
            })
            if automatic:
                set_quota(user_id, quota)
                _start_cooldown(user_id)
    except sqlite3.IntegrityError:
        raise GovernanceError("page_governance.quota.error.already_pending") from None
    row = get_request(request_id)
    assert row is not None
    if row["status"] == "pending":
        attention.created("page_governance.quota_requests", request_id)
    return row


def review(request_id: int, reviewer_id: str, *, approve: bool, reason: str = "") -> dict[str, Any]:
    reason = (reason or "").strip()
    if len(reason) > MAX_REVIEW_REASON:
        raise GovernanceError("page_governance.quota.error.reason_too_long", limit=MAX_REVIEW_REASON)
    with db.transaction():
        row = db.one("SELECT * FROM reservation_quota_requests WHERE id = ?", (request_id,))
        if row is None:
            raise GovernanceError("page_governance.quota.error.missing")
        if row["status"] != "pending":
            raise GovernanceError("page_governance.quota.error.already_reviewed")
        db.update("reservation_quota_requests", {
            "status": "approved" if approve else "denied", "reviewed_by": reviewer_id, "review_reason": reason,
            "review_source": "manual", "reviewed_at": now_sql(),
        }, "id = ?", (request_id,))
        if approve:
            set_quota(row["user_id"], row["requested_quota"])
        _start_cooldown(row["user_id"])
    reviewed = get_request(request_id)
    assert reviewed is not None
    attention.decided(reviewed["user_id"],
                      "page_governance.quota_requests", reviewed["status"],
                      object_id=request_id, endpoint="page_governance.my_quota")
    return reviewed


def cancel(request_id: int, user_id: str) -> None:
    """Withdraw one's own pending request."""
    with db.transaction():
        changed = db.execute(
            "UPDATE reservation_quota_requests SET status = 'cancelled', reviewed_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'pending'",
            (now_sql(), request_id, user_id),
        ).rowcount
        if not changed:
            raise GovernanceError("page_governance.quota.error.missing")
        _start_cooldown(user_id)
