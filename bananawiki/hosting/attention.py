"""What is waiting for a portal administrator, and notices about decisions.

The portal's approval queues (sources):

* ``accounts.pending``   sign-ups waiting for approval (approval sign-up mode
                         or hosting activation);
* ``features.requests``  wiki feature requests (public access, page builder);
* ``merges.awaiting``    account merges both owners confirmed, waiting for an
                         administrator to run them.

Counts are one indexed ``COUNT`` each, cached for the request. The admin
menu's red dot, the dashboard list, the banner after signing in and the
``/admin/attention`` page read :func:`items`; the maintenance service emails
administrators (see :mod:`.notifications`). Services call :func:`created` when
a new item arrives and :func:`decided` when an owner's own request was
approved or denied, which leaves a notice on their dashboard (and an email).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flask import g, has_request_context, url_for

from ..core import attention as engine
from ..core.timeutil import now_sql, sql_in
from .db import _session, db

EVENTS_TABLE = "hosting_attention_events"
RECIPIENTS_TABLE = "hosting_attention_recipients"
OUTCOMES = ("approved", "denied")
NOTICE_RETENTION_DAYS = 90
MAX_NOTICES_SHOWN = 3


@dataclass(frozen=True)
class Source:
    id: str
    label: str  # translation key
    endpoint: str
    count: Callable[[], int]
    oldest: Callable[[], str | None]
    endpoint_args: dict[str, Any] = field(default_factory=dict)


SOURCES = (
    Source("accounts.pending", "hosting.attention.source.accounts.pending", "admin.dashboard",
           lambda: int(db.scalar("SELECT COUNT(*) FROM accounts WHERE approval_status = 'pending' "
                               "AND deleted_at IS NULL") or 0),
           lambda: db.scalar("SELECT MIN(created_at) FROM accounts WHERE approval_status = 'pending' "
                           "AND deleted_at IS NULL"),
           {"_anchor": "pending-accounts"}),
    Source("features.requests", "hosting.attention.source.features.requests", "admin.dashboard",
           lambda: int(db.scalar("SELECT COUNT(*) FROM instance_feature_requests WHERE status = 'pending'") or 0),
           lambda: db.scalar("SELECT MIN(requested_at) FROM instance_feature_requests WHERE status = 'pending'"),
           {"_anchor": "feature-requests"}),
    Source("merges.awaiting", "hosting.attention.source.merges.awaiting", "admin.merge_requests",
           lambda: int(db.scalar("SELECT COUNT(*) FROM hosting_account_merge_requests WHERE status = 'approved'") or 0),
           lambda: db.scalar("SELECT MIN(created_at) FROM hosting_account_merge_requests WHERE status = 'approved'")),
)


def counts() -> dict[str, int]:
    """``source id -> count`` (what every administrator can act on)."""
    return {source.id: source.count() for source in SOURCES}


def items(account: dict[str, Any] | None, *, with_oldest: bool = False) -> list[dict[str, Any]]:
    """The non-empty queues for an administrator (nothing for anyone else), cached per request."""
    if not account or not account.get("is_admin"):
        return []
    cached = g.get("_attention") if has_request_context() else None
    if cached is None:
        cached = [{"id": source.id, "label": source.label, "count": number, "source": source,
                   "url": url_for(source.endpoint, **source.endpoint_args)}
                  for source, number in ((source, source.count()) for source in SOURCES) if number]
        if has_request_context():
            g._attention = cached
    if not with_oldest:
        return cached
    return [{**entry, "oldest": entry["source"].oldest()} for entry in cached]


def total(account: dict[str, Any] | None) -> int:
    return sum(entry["count"] for entry in items(account))


def store() -> engine.Store:
    return engine.Store(_session(), EVENTS_TABLE, RECIPIENTS_TABLE)


def created(source_id: str, object_id: Any = "") -> None:
    """A new item arrived: the next maintenance pass reacts to it even if the count did not change."""
    store().record_event(source_id, "" if object_id is None else object_id)


# ── Notices to owners ────────────────────────────────────────────────────────


def decided(account_id: str | None, source_id: str, outcome: str, *, object_id: Any = "", detail: str = "",
            email: bool = True) -> None:
    """Leave a notice for *account_id* about the decision on their request.

    *detail* is a short, non-secret label (the wiki's address, the feature's
    name) shown in the notice. ``email=False`` marks it as already emailed
    (for decisions that send their own message).
    """
    if not account_id or outcome not in OUTCOMES:
        return
    db.insert("hosting_account_notices", {
        "account_id": account_id, "source_id": source_id[:100], "outcome": outcome,
        "object_id": str(object_id if object_id is not None else "")[:100], "detail": (detail or "")[:200],
        "created_at": now_sql(), "emailed_at": None if email else now_sql(),
    })


def notices_for(account_id: str, limit: int = MAX_NOTICES_SHOWN) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM hosting_account_notices WHERE account_id = ? AND dismissed_at IS NULL "
                  "ORDER BY id DESC LIMIT ?", (account_id, limit))


def dismiss(account_id: str, notice_id: int | None = None) -> int:
    if notice_id is None:
        return db.execute("UPDATE hosting_account_notices SET dismissed_at = ? WHERE account_id = ? "
                          "AND dismissed_at IS NULL", (now_sql(), account_id)).rowcount
    return db.execute("UPDATE hosting_account_notices SET dismissed_at = ? WHERE id = ? AND account_id = ? "
                      "AND dismissed_at IS NULL", (now_sql(), notice_id, account_id)).rowcount


def prune() -> int:
    removed = db.execute("DELETE FROM hosting_account_notices WHERE created_at < ?",
                         (sql_in(days=-NOTICE_RETENTION_DAYS),)).rowcount
    return removed + store().prune()
