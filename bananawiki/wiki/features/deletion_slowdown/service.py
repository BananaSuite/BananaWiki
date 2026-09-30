"""Deletion slowdown: deleting a page first hides it for a grace period.

A deleted page is marked ``pending_deletion`` (with who and when). The pages
service already hides such pages from everyone without ``page.delete`` and
refuses to edit them. After :data:`GRACE_HOURS` the purge job deletes the
page for good through the pages service (so ``page.deleted`` fires for every
purge); until then it can be restored.

With ``docs_bypass_deletion_slowdown`` set, pages in the documentation
category (``docs_category_id``) are deleted at once, as in 1.4.

When the feature is switched off nothing is purged (its job does not run)
and deleting a page deletes it immediately. Pages that were already waiting
stay hidden and waiting; holders of ``page.delete`` still see them and can
delete them right away, and switching the feature back on resumes the
countdown from the original deletion time (pages whose grace period has
passed are purged at the next run).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ....core.timeutil import now_sql, parse, sql_in, to_sql
from ... import attention, settings
from ...db import db
from ...registry import emit
from ..pages import service as pages

GRACE_HOURS = 48


def purge_at(pending_since: str | None) -> str | None:
    """When a page marked pending at *pending_since* will be purged."""
    moment = parse(pending_since)
    return to_sql(moment + timedelta(hours=GRACE_HOURS)) if moment else None


def bypasses(page: dict[str, Any]) -> bool:
    """Documentation pages skip the grace period when the site says so."""
    docs = settings.get("docs_category_id")
    return bool(settings.get("docs_bypass_deletion_slowdown")) and docs is not None \
        and page.get("category_id") is not None and int(docs) == int(page["category_id"])


def schedule(page: dict[str, Any], actor_id: str | None) -> bool:
    """Mark *page* pending deletion. False when it already is (or is the home page)."""
    if page.get("is_home"):
        return False
    with db.transaction():
        current = pages.get(page["id"], with_content=False)
        if current is None or current["pending_deletion"] or current["is_home"]:
            return False
        pages.set_fields(page["id"], pending_deletion=1, pending_deletion_by=actor_id, pending_deletion_at=now_sql())
    attention.created("deletion_slowdown.pending", page["id"])
    return True


def restore(page: dict[str, Any], actor_id: str | None) -> bool:
    with db.transaction():
        current = pages.get(page["id"], with_content=False)
        if current is None or not current["pending_deletion"]:
            return False
        pages.set_fields(page["id"], pending_deletion=0, pending_deletion_by=None, pending_deletion_at=None)
    restored = pages.get(page["id"])
    emit("page.restored", page=restored, actor_id=actor_id)
    return True


def purge(page: dict[str, Any], actor_id: str | None) -> bool:
    """Delete a pending page now (administrators, or the purge job)."""
    with db.transaction():
        current = pages.get(page["id"])
        if current is None or not current["pending_deletion"]:
            return False
        pages.delete(current, actor_id=actor_id)
    return True


def list_pending() -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT p.id, p.title, p.slug, p.category_id, p.pending_deletion_by, p.pending_deletion_at, "
        "u.username AS deleted_by_name, c.name AS category_name FROM pages p "
        "LEFT JOIN users u ON u.id = p.pending_deletion_by LEFT JOIN categories c ON c.id = p.category_id "
        "WHERE p.pending_deletion = 1 ORDER BY p.pending_deletion_at DESC, p.id DESC"
    )
    return [{**row, "purge_at": purge_at(row["pending_deletion_at"])} for row in rows]


def count_pending() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM pages WHERE pending_deletion = 1", default=0))


def oldest_pending() -> str | None:
    return db.scalar("SELECT MIN(pending_deletion_at) FROM pages WHERE pending_deletion = 1")


def purge_due() -> int:
    """The purge job: delete pages whose grace period has passed (idempotent, one page per transaction)."""
    cutoff = sql_in(hours=-GRACE_HOURS)
    purged = 0
    for page_id in db.column(
        "SELECT id FROM pages WHERE pending_deletion = 1 AND pending_deletion_at IS NOT NULL "
        "AND pending_deletion_at <= ? AND is_home = 0 ORDER BY id",
        (cutoff,),
    ):
        with db.transaction():
            current = pages.get(page_id)
            if (current is None or not current["pending_deletion"] or current["is_home"]
                    or (current["pending_deletion_at"] or "") > cutoff):
                continue
            pages.delete(current, actor_id=current["pending_deletion_by"])
        purged += 1
    return purged
