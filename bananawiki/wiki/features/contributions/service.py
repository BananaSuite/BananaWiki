"""Proposed edits ("contributions") from people who may read a page but not edit it.

A user with ``contribution.propose`` who can see a page but cannot edit it
proposes a new title and content with a reason. Reviewers (``contribution.review``
plus edit rights on that page; administrators always) approve or deny it.
Approving applies the edit through the pages service, credited to the
proposer. Each user may have at most their quota of proposals waiting.

``pending_contributions`` keeps one row per (page, user) (``UNIQUE`` in the
1.4 schema). 1.4 therefore refused a second proposal for a page until the
old row was purged; here a new proposal reuses the resolved row.
``base_revision`` records the page revision the proposal started from so
reviewers can see when the page changed since. An approval is bound to what
the reviewer looked at: the review form carries :func:`review_version` and
the page revision, and :func:`approve` refuses if either moved on meanwhile.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ....core.timeutil import now_sql, sql_in
from ... import attention, auth, settings
from ...db import db
from ...registry import intercept
from ..pages import diff as page_diff
from ..pages import service as pages
from . import quota
from .errors import ContributionError

MAX_REASON = 2000
MAX_REVIEW_REASON = 500
RESOLVED_RETENTION_DAYS = 90
DIFF_CONTEXT = 3
MAX_DIFF_ROWS = 5000


# ── Who may do what ─────────────────────────────────────────────────────────


def can_propose(page: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
    return bool(
        page and user and auth.has_permission("contribution.propose", user) and pages.can_view(page, user)
        and not pages.can_edit(page, user) and not page.get("pending_deletion")
    )


def can_review(page: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
    return bool(page and user and auth.has_permission("contribution.review", user) and pages.can_edit(page, user))


def is_reviewer(user: dict[str, Any] | None) -> bool:
    return bool(user) and auth.has_role("editor", user) and auth.has_permission("contribution.review", user)


# ── Reading ──────────────────────────────────────────────────────────────────

_SELECT = (
    "SELECT c.*, u.username, p.title AS page_title, p.slug AS page_slug, p.revision AS page_revision, "
    "r.username AS reviewed_by_username FROM pending_contributions c "
    "JOIN users u ON u.id = c.user_id JOIN pages p ON p.id = c.page_id "
    "LEFT JOIN users r ON r.id = c.reviewed_by"
)


def get(contribution_id: int) -> dict[str, Any] | None:
    return db.one(f"{_SELECT} WHERE c.id = ?", (contribution_id,))


def own_pending(page_id: int, user_id: str) -> dict[str, Any] | None:
    return db.one(f"{_SELECT} WHERE c.page_id = ? AND c.user_id = ? AND c.status = 'pending'", (page_id, user_id))


def of_user(user_id: str) -> list[dict[str, Any]]:
    return db.all(f"{_SELECT} WHERE c.user_id = ? ORDER BY c.updated_at DESC, c.id DESC", (user_id,))


def pending_on_page(page_id: int) -> list[dict[str, Any]]:
    return db.all(f"{_SELECT} WHERE c.page_id = ? AND c.status = 'pending' ORDER BY c.created_at", (page_id,))


def pending_count(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM pending_contributions WHERE user_id = ? AND status = 'pending'", (user_id,), default=0
    ))


def pending_for_reviewer(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Waiting proposals on pages *user* may review, oldest first."""
    rows = db.all(f"{_SELECT} WHERE c.status = 'pending' ORDER BY c.created_at, c.id")
    if auth.is_admin(user):
        return rows
    readable: dict[int, bool] = {}
    result = []
    for row in rows:
        if row["page_id"] not in readable:
            readable[row["page_id"]] = can_review(pages.get(row["page_id"], with_content=False), user)
        if readable[row["page_id"]]:
            result.append(row)
    return result


def review_count(user: dict[str, Any]) -> int:
    if auth.is_admin(user):
        return int(db.scalar("SELECT COUNT(*) FROM pending_contributions WHERE status = 'pending'", default=0))
    return len(pending_for_reviewer(user))


def oldest_for_reviewer(user: dict[str, Any]) -> str | None:
    """When the proposal waiting longest for *user* was filed."""
    if auth.is_admin(user):
        return db.scalar("SELECT MIN(created_at) FROM pending_contributions WHERE status = 'pending'")
    rows = pending_for_reviewer(user)
    return rows[0]["created_at"] if rows else None


# ── Proposing ────────────────────────────────────────────────────────────────


def _clean(page: dict[str, Any], title: str | None, content: str | None, reason: str | None) -> tuple[str, str, str]:
    title = " ".join((title or "").split()) or page["title"]
    if len(title) > pages.MAX_TITLE:
        raise ContributionError("contributions.error.title_too_long", limit=pages.MAX_TITLE)
    content = (content or "").replace("\r\n", "\n")
    if len(content) > pages.MAX_CONTENT:
        raise ContributionError("contributions.error.content_too_long")
    reason = (reason or "").strip()
    if not reason:
        raise ContributionError("contributions.error.reason_required")
    if len(reason) > MAX_REASON:
        raise ContributionError("contributions.error.reason_too_long", limit=MAX_REASON)
    return title, content, reason


def propose(page: dict[str, Any], user: dict[str, Any], *, title: str | None, content: str | None,
            reason: str | None) -> int:
    """Store a proposal atomically (one waiting proposal per page, quota enforced)."""
    if not can_propose(page, user):
        raise ContributionError("contributions.error.cannot_propose")
    title, content, reason = _clean(page, title, content, reason)
    with db.transaction():
        existing = db.one("SELECT id, status FROM pending_contributions WHERE page_id = ? AND user_id = ?",
                          (page["id"], user["id"]))
        if existing and existing["status"] == "pending":
            raise ContributionError("contributions.error.already_pending")
        limit = quota.effective(user["id"])
        if limit != quota.UNLIMITED and pending_count(user["id"]) >= limit:
            raise ContributionError("contributions.error.quota", quota=limit)
        now = now_sql()
        values = {
            "title": title, "content": content, "reason": reason, "status": "pending", "reviewed_by": None,
            "review_reason": "", "review_source": "manual", "reviewed_at": None, "created_at": now,
            "updated_at": now, "base_revision": page.get("revision"),
        }
        if existing:
            db.update("pending_contributions", values, "id = ?", (existing["id"],))
            contribution_id = int(existing["id"])
        else:
            contribution_id = db.insert("pending_contributions", {"page_id": page["id"], "user_id": user["id"], **values})
    attention.created("contributions.reviews", contribution_id)
    return contribution_id


def update_own(contribution: dict[str, Any], user: dict[str, Any], *, title: str | None, content: str | None,
               reason: str | None) -> None:
    if contribution["user_id"] != user["id"]:
        raise ContributionError("contributions.error.not_yours")
    page = pages.get(contribution["page_id"])
    assert page is not None
    title, content, reason = _clean(page, title, content, reason)
    changed = db.execute(
        "UPDATE pending_contributions SET title = ?, content = ?, reason = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND status = 'pending'",
        (title, content, reason, now_sql(), contribution["id"], user["id"]),
    ).rowcount
    if not changed:
        raise ContributionError("contributions.error.not_pending")


def withdraw(contribution: dict[str, Any], user: dict[str, Any]) -> None:
    changed = db.execute(
        "UPDATE pending_contributions SET status = 'withdrawn', updated_at = ? "
        "WHERE id = ? AND user_id = ? AND status = 'pending'",
        (now_sql(), contribution["id"], user["id"]),
    ).rowcount
    if not changed:
        raise ContributionError("contributions.error.not_pending")


# ── Reviewing ────────────────────────────────────────────────────────────────


def _review_target(contribution_id: int, reviewer: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    contribution = get(contribution_id)
    if contribution is None or contribution["status"] != "pending":
        raise ContributionError("contributions.error.not_pending")
    page = pages.get(contribution["page_id"])
    if not can_review(page, reviewer):
        raise ContributionError("contributions.error.cannot_review")
    assert page is not None
    return contribution, page


def _check_review_reason(reason: str | None) -> str:
    reason = (reason or "").strip()
    if len(reason) > MAX_REVIEW_REASON:
        raise ContributionError("contributions.error.reason_too_long", limit=MAX_REVIEW_REASON)
    return reason


def review_version(contribution: dict[str, Any]) -> str:
    """Fingerprint of what a reviewer is shown and an approval applies (title, content, reason)."""
    shown = json.dumps([contribution["title"], contribution["content"], contribution["reason"]])
    return hashlib.sha256(shown.encode()).hexdigest()


def approve(contribution_id: int, reviewer: dict[str, Any], note: str = "", *,
            reviewed_version: str | None = None, page_revision: int | None = None) -> dict[str, Any]:
    """Apply the proposal to the page, credited to the proposer; returns the updated page.

    The status change and the page edit commit together or not at all.
    *reviewed_version* (:func:`review_version`) and *page_revision* are what the
    reviewer looked at: if the proposer edited the proposal or someone saved
    the page since, nothing is applied. The review form always sends both;
    ``None`` skips that check.
    """
    note = _check_review_reason(note)
    try:
        with db.transaction():
            contribution, page = _review_target(contribution_id, reviewer)
            if reviewed_version is not None and reviewed_version != review_version(contribution):
                raise ContributionError("contributions.error.changed_since_review")
            if page.get("pending_deletion"):
                raise ContributionError("contributions.error.page_pending_deletion")
            blocked = intercept("page.edit_blocked", page=page, user=reviewer)
            if blocked:
                raise ContributionError(blocked)
            now = now_sql()
            claimed = db.execute(
                "UPDATE pending_contributions SET status = 'approved', reviewed_by = ?, review_reason = ?, "
                "review_source = 'manual', reviewed_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (reviewer["id"], note, now, now, contribution_id),
            ).rowcount
            if not claimed:
                raise ContributionError("contributions.error.not_pending")
            message = f"Contribution #{contribution_id} by {contribution['username']}, approved by " \
                      f"{reviewer['username']}: {contribution['reason']}"
            updated = pages.update(page, author_id=contribution["user_id"],
                                   title=contribution["title"] or page["title"], content=contribution["content"],
                                   edit_message=message[:pages.MAX_EDIT_MESSAGE], expected_revision=page_revision)
    except pages.EditConflict:
        raise ContributionError("contributions.error.page_changed_since_review") from None
    except pages.PageError as exc:
        raise ContributionError(exc.key, **exc.values) from None
    attention.decided(contribution["user_id"], "contributions.reviews", "approved", object_id=contribution_id,
                      endpoint="contributions.mine")
    return updated


def deny(contribution_id: int, reviewer: dict[str, Any], reason: str = "") -> None:
    reason = _check_review_reason(reason)
    with db.transaction():
        contribution, _page = _review_target(contribution_id, reviewer)
        now = now_sql()
        db.execute(
            "UPDATE pending_contributions SET status = 'denied', reviewed_by = ?, review_reason = ?, "
            "review_source = 'manual', reviewed_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (reviewer["id"], reason, now, now, contribution_id),
        )
    attention.decided(contribution["user_id"], "contributions.reviews", "denied", object_id=contribution_id,
                      endpoint="contributions.mine")


# ── Diff for reviewers ───────────────────────────────────────────────────────


def diff_lines(old: str, new: str) -> list[dict[str, str]]:
    """Line diff with a few lines of context; long unchanged runs become a gap marker.

    The comparison has the work budget of the pages diff. When the texts are
    too different for it, the rows start with a ``coarse`` marker and part of
    the text shows as removed and added as a whole. At most
    :data:`MAX_DIFF_ROWS` rows are listed; a final ``more`` row counts the
    changed lines left out, so a huge proposal still opens quickly.
    """
    before, after = (old or "").splitlines(), (new or "").splitlines()
    ops, complete = page_diff.opcodes(before, after, page_diff.Budget())
    rows: list[dict[str, str]] = []
    hidden = 0

    def add(op: str, lines: list[str], start: int, stop: int, room: int) -> None:
        nonlocal hidden
        shown = min(stop, start + max(0, room))
        rows.extend({"op": op, "text": line} for line in lines[start:shown])
        if op != "equal":
            hidden += stop - shown

    for group in page_diff.grouped_opcodes(ops, DIFF_CONTEXT):
        if rows and len(rows) < MAX_DIFF_ROWS:
            rows.append({"op": "gap", "text": ""})
        for tag, i1, i2, j1, j2 in group:
            room = MAX_DIFF_ROWS - len(rows)
            if tag == "equal":
                add("equal", before, i1, i2, room)
                continue
            add("del", before, i1, i2, room - min(j2 - j1, room // 2))  # leave room for the proposed text
            add("add", after, j1, j2, MAX_DIFF_ROWS - len(rows))
    if hidden:
        rows.append({"op": "more", "text": str(hidden)})
    return rows if complete else [{"op": "coarse", "text": ""}, *rows]


def withdraw_if_editing_allowed(user: dict[str, Any], old_role: str, new_role: str,
                                changed_by: str | None) -> None:
    """``user.role_changed``: someone who may now edit directly no longer needs their proposals (as in 1.4)."""
    if not (auth.has_role("editor", user) and auth.has_permission("page.edit_all", user)):
        return
    db.execute(
        "UPDATE pending_contributions SET status = 'withdrawn', review_reason = ?, updated_at = ? "
        "WHERE user_id = ? AND status = 'pending'",
        ("Withdrawn automatically: the author can now edit pages directly.", now_sql(), user["id"]),
    )


# ── Housekeeping ─────────────────────────────────────────────────────────────


def expire() -> None:
    """Expire old proposals and drop long-resolved rows (idempotent).

    As in 1.4, proposals expire after ``draft_expiration_hours`` (0 = never).
    """
    now = now_sql()
    try:
        hours = max(0, int(settings.get("draft_expiration_hours", 0)))
    except (TypeError, ValueError):
        hours = 0
    with db.transaction():
        if hours:
            db.execute(
                "UPDATE pending_contributions SET status = 'expired', updated_at = ? "
                "WHERE status = 'pending' AND created_at < ?",
                (now, sql_in(hours=-hours)),
            )
        cutoff = sql_in(days=-RESOLVED_RETENTION_DAYS)
        db.execute("DELETE FROM pending_contributions WHERE status != 'pending' AND updated_at < ?", (cutoff,))
        db.execute("DELETE FROM contribution_quota_requests WHERE status != 'pending' AND created_at < ?", (cutoff,))
