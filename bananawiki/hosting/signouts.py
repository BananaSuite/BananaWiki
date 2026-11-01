"""Single sign-out: the portal tells its wikis to end an account's platform sessions.

Wikis keep their own sessions, so a portal account that changes its password,
is suspended, deleted, scheduled for deletion or merged away, or that signs
out of a wiki from its account page, would otherwise stay signed in to every
wiki it reached through platform sign-in. Each of those queues one row per
wiki the account is known to (a link to a wiki account, a remembered consent
or a live access token) in ``hosting_oauth_signouts``. A repeated event
replaces the identical older one, so the queue holds at most one event per
wiki, account, wiki account and reason, however often an account signs out.

Wikis fetch their rows with their client credentials
(``GET /oauth/signouts?after=<id>``, see :mod:`.routes.oauth`) every minute
and end the sessions that the linked wiki account started through platform
sign-in *before* the row was written. Fetching a row twice, or every row
again after the portal was restored from a backup, therefore never ends a
session that started later. For ``owner_changed`` (the account no longer owns
the wiki) the wiki also drops its own link to the wiki account when that link
is no newer than the row, as the portal drops its copy: a former owner who
had connected the wiki's administrator account must not keep signing in as it. Nothing is pushed to the wikis: the portal never
opens connections to tenant containers.
"""

from __future__ import annotations

from typing import Any

from ..core.timeutil import now_sql, sql_in
from .db import db

RETENTION_DAYS = 30
PAGE_SIZE = 200
REASONS = frozenset({
    "signed_out", "access_removed", "password_changed", "suspended", "deleted", "deletion_scheduled", "merged",
    "owner_changed",
})


def _wikis_of(account_id: str, instance_id: str | None) -> list[dict[str, Any]]:
    """The wikis that may hold sessions of *account_id*, with the wiki account the portal links it to there."""
    where = " AND i.id = ?" if instance_id else ""
    params: list[Any] = [account_id, account_id, account_id]
    if instance_id:
        params.append(instance_id)
    return db.all(
        "SELECT i.id AS instance_id, COALESCE(l.wiki_user_id, '') AS wiki_user_id FROM instances i "
        "LEFT JOIN hosting_oauth_account_links l ON l.instance_id = i.id AND l.account_id = ? "
        "WHERE (l.id IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM hosting_oauth_grants g WHERE g.instance_id = i.id AND g.account_id = ?) "
        "OR EXISTS (SELECT 1 FROM hosting_oauth_access_tokens t WHERE t.client_id = i.oauth_client_id "
        "AND t.account_id = ?))" + where,
        params,
    )


def queue(account_id: str, reason: str, *, instance_id: str | None = None) -> int:
    """Ask every wiki of *account_id* (or only *instance_id*) to sign it out; returns how many were asked."""
    if reason not in REASONS:
        raise ValueError(f"Unknown sign-out reason: {reason}")
    if not account_id:
        return 0
    now = now_sql()
    rows = _wikis_of(account_id, instance_id)
    with db.transaction():
        for row in rows:
            wiki_user_id = str(row["wiki_user_id"])[:128]
            # A newer event reaches every session and link an identical older one does, so repeated
            # sign-outs replace it: an account keeps at most one event per wiki, wiki account and reason.
            # Ids are AUTOINCREMENT, so the new event's id is always beyond the wiki's cursor.
            db.execute("DELETE FROM hosting_oauth_signouts WHERE instance_id = ? AND account_id = ? "
                       "AND wiki_user_id = ? AND reason = ?", (row["instance_id"], account_id, wiki_user_id, reason))
            db.insert("hosting_oauth_signouts", {"instance_id": row["instance_id"], "account_id": account_id,
                                                 "wiki_user_id": wiki_user_id, "reason": reason, "created_at": now})
    return len(rows)


def pending(instance_id: str, after: int, limit: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    """``(events after *after*, the latest event id of the wiki)`` for one wiki, oldest first.

    A wiki whose cursor is beyond the latest id (the portal was restored
    from an older backup) starts again from 0.
    """
    limit = max(1, min(int(limit), PAGE_SIZE))
    rows = db.all(
        "SELECT id, account_id, wiki_user_id, reason, created_at FROM hosting_oauth_signouts "
        "WHERE instance_id = ? AND id > ? ORDER BY id LIMIT ?", (instance_id, max(0, int(after)), limit),
    )
    latest = int(db.scalar("SELECT MAX(id) FROM hosting_oauth_signouts WHERE instance_id = ?", (instance_id,),
                           default=0) or 0)
    return rows, latest


def prune(retention_days: int = RETENTION_DAYS) -> int:
    return db.execute("DELETE FROM hosting_oauth_signouts WHERE created_at < ?",
                      (sql_in(days=-retention_days),)).rowcount
