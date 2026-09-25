"""Draft management."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def save_draft(page_id, user_id, title, content):
    """Insert or replace a draft for the given (page, user) pair."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO drafts (page_id, user_id, title, content, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(page_id, user_id) DO UPDATE SET title=?, content=?, updated_at=?",
            (page_id, user_id, title, content, now, title, content, now),
        )
        conn.commit()


@retry_on_busy
def get_draft(page_id, user_id):
    """Return the draft for a specific (page, user) pair, or None if none exists."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE page_id=? AND user_id=?", (page_id, user_id)
        ).fetchone()
    return row


@retry_on_busy
def get_drafts_for_page(page_id):
    """Return all drafts for a given page, with editor usernames joined."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT d.*, u.username FROM drafts d JOIN users u ON d.user_id=u.id WHERE d.page_id=?",
            (page_id,),
        ).fetchall()
    return rows


def delete_draft(page_id, user_id):
    """Delete the draft for the given (page, user) pair."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM drafts WHERE page_id=? AND user_id=?", (page_id, user_id))
        conn.commit()


def transfer_draft(page_id, from_user, to_user):
    """Transfer a draft from one user to another (atomic).

    Deletes the target user's existing draft (if any) and transfers the
    source user's draft.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM drafts WHERE page_id=? AND user_id=?", (page_id, to_user))
        conn.execute(
            "UPDATE drafts SET user_id=?, updated_at=? WHERE page_id=? AND user_id=?",
            (to_user, now, page_id, from_user),
        )
        conn.commit()


@retry_on_busy
def get_user_draft_count(user_id):
    """Return number of pending drafts for a user across all pages."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM drafts WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["cnt"] if row else 0


@retry_on_busy
def list_user_drafts(user_id):
    """Return all drafts belonging to a user, with page info."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT d.*, p.title AS page_title, p.slug AS page_slug, p.category_id AS page_category_id "
            "FROM drafts d JOIN pages p ON d.page_id = p.id "
            "WHERE d.user_id=? ORDER BY d.updated_at DESC",
            (user_id,),
        ).fetchall()
    return rows


def save_page_builder_draft(page_id, user_id, builder_json):
    """Upsert one user's isolated visual-builder draft."""
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO page_builder_drafts (page_id, user_id, builder_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(page_id, user_id) DO UPDATE SET "
            "builder_json=excluded.builder_json, updated_at=datetime('now')",
            (page_id, user_id, builder_json),
        )
        conn.commit()


def save_page_builder_draft_if_current(
    page_id, user_id, builder_json, expected_last_edited_at
):
    """Atomically save a builder draft only for the expected page revision."""
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        page = conn.execute(
            "SELECT last_edited_at FROM pages WHERE id=?", (page_id,)
        ).fetchone()
        current_revision = (page["last_edited_at"] if page else None) or ""
        if not page or current_revision != (expected_last_edited_at or ""):
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO page_builder_drafts (page_id, user_id, builder_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(page_id, user_id) DO UPDATE SET "
            "builder_json=excluded.builder_json, updated_at=datetime('now')",
            (page_id, user_id, builder_json),
        )
        conn.commit()
        return True


@retry_on_busy
def get_page_builder_draft(page_id, user_id):
    """Return the builder draft owned by this user for this page."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM page_builder_drafts WHERE page_id=? AND user_id=?",
            (page_id, user_id),
        ).fetchone()


def delete_page_builder_draft(page_id, user_id):
    """Delete only this user's builder draft for the page."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM page_builder_drafts WHERE page_id=? AND user_id=?",
            (page_id, user_id),
        )
        conn.commit()
