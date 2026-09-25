"""Page CRUD, history, search, sequential nav, and page attachments."""

import re
import sqlite3
from datetime import datetime, timezone

from ._connection import SYSTEM_USER_ID, get_db_context, retry_on_busy


_NO_EXPECTED_REVISION = object()


def update_category_sequential_nav(cat_id, enabled):
    """Enable or disable sequential prev/next navigation for pages in a category."""
    with get_db_context() as conn:
        conn.execute("UPDATE categories SET sequential_nav=? WHERE id=?", (1 if enabled else 0, cat_id))
        conn.commit()


@retry_on_busy
def get_adjacent_pages(page_id):
    """Return (prev_page, next_page) for sequential navigation within the same category.

    Both values are sqlite3 Row objects (with id, title, slug) or None.
    Returns (None, None) if the page's category does not have sequential_nav enabled.
    """
    with get_db_context() as conn:
        row = conn.execute("SELECT category_id, sort_order FROM pages WHERE id=?", (page_id,)).fetchone()
        if not row or not row["category_id"]:
            return None, None
        cat = conn.execute("SELECT sequential_nav FROM categories WHERE id=?",
                           (row["category_id"],)).fetchone()
        if not cat or not cat["sequential_nav"]:
            return None, None
        cat_id = row["category_id"]
        sort_order = row["sort_order"]
        prev_page = conn.execute(
            "SELECT id, title, slug, category_id, is_deindexed, pending_deletion, builder_json, builder_public "
            "FROM pages WHERE category_id=? AND sort_order<? AND is_home=0 AND is_deindexed=0 AND pending_deletion=0 "
            "ORDER BY sort_order DESC LIMIT 1",
            (cat_id, sort_order),
        ).fetchone()
        next_page = conn.execute(
            "SELECT id, title, slug, category_id, is_deindexed, pending_deletion, builder_json, builder_public "
            "FROM pages WHERE category_id=? AND sort_order>? AND is_home=0 AND is_deindexed=0 AND pending_deletion=0 "
            "ORDER BY sort_order ASC LIMIT 1",
            (cat_id, sort_order),
        ).fetchone()
        return prev_page, next_page


def update_page_slug(page_id, new_slug):
    """Change a page's URL slug and rewrite all internal links in other pages' content.

    Any occurrence of /page/<old_slug> in page content is updated to /page/<new_slug>.
    Returns the old slug so callers can redirect.
    """
    with get_db_context() as conn:
        old_row = conn.execute("SELECT slug, is_home FROM pages WHERE id=?", (page_id,)).fetchone()
        if not old_row:
            return None
        if old_row["is_home"] and old_row["slug"] != new_slug:
            return None
        old_slug = old_row["slug"]
        if old_slug == new_slug:
            return old_slug
        # Update the slug on the page itself
        conn.execute("UPDATE pages SET slug=? WHERE id=?", (new_slug, page_id))
        # Rewrite internal links in all other pages' content.
        # Use a regex so that /page/notes is replaced only when NOT followed
        # by another slug character ([A-Za-z0-9_-]), preventing corruption of
        # links like /page/notes-archive when renaming "notes" → "my-notes".
        old_link = f"/page/{old_slug}"
        new_link = f"/page/{new_slug}"
        old_pattern = re.compile(
            re.escape(old_link) + r"(?=[^A-Za-z0-9_-]|$)"
        )
        like_pat = f"%{_escape_like(old_link)}%"
        pages = conn.execute(
            "SELECT id, content FROM pages WHERE id!=? AND content LIKE ? ESCAPE '\\'",
            (page_id, like_pat),
        ).fetchall()
        for p in pages:
            if old_link in (p["content"] or ""):
                updated = old_pattern.sub(new_link, p["content"])
                if updated != p["content"]:
                    conn.execute("UPDATE pages SET content=? WHERE id=?", (updated, p["id"]))
        # Also rewrite in any open drafts
        drafts = conn.execute(
            "SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'",
            (like_pat,),
        ).fetchall()
        for d in drafts:
            if old_link in (d["content"] or ""):
                updated = old_pattern.sub(new_link, d["content"])
                if updated != d["content"]:
                    conn.execute("UPDATE drafts SET content=? WHERE id=?", (updated, d["id"]))
        conn.commit()
        return old_slug


def _escape_like(value):
    """Escape SQLite LIKE metacharacters so they are matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@retry_on_busy
def search_pages(query, limit=15, include_deindexed=False):
    """Return pages whose title matches *query* (case-insensitive prefix/substring).

    Used by the link-insertion autocomplete endpoint.  Deindexed pages are
    excluded by default; pass ``include_deindexed=True`` for editors/admins.
    """
    with get_db_context() as conn:
        pattern = f"%{_escape_like(query)}%"
        deindex_clause = "" if include_deindexed else " AND is_deindexed=0"
        rows = conn.execute(
            f"SELECT id, title, slug, category_id, is_deindexed, builder_json, builder_public FROM pages "
            f"WHERE is_home=0 AND pending_deletion=0{deindex_clause} AND title LIKE ? ESCAPE '\\' "
            "ORDER BY title LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return rows


@retry_on_busy
def search_pages_full(query, limit=20, include_deindexed=False, search_content=False):
    """Return pages matching *query* in title and optionally content.

    Returns dicts with ``id``, ``title``, ``slug`` and ``category_id``.
    Deindexed pages are excluded by default.
    When ``search_content`` is True, also matches within page body text.
    """
    with get_db_context() as conn:
        pattern = f"%{_escape_like(query)}%"
        if search_content:
            if include_deindexed:
                sql = (
                    "SELECT id, title, slug, category_id, builder_json, builder_public FROM pages "
                    "WHERE is_home=0 AND pending_deletion=0 AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') "
                    "ORDER BY CASE WHEN title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, title LIMIT ?"
                )
            else:
                sql = (
                    "SELECT id, title, slug, category_id, builder_json, builder_public FROM pages "
                    "WHERE is_home=0 AND pending_deletion=0 AND is_deindexed=0 AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') "
                    "ORDER BY CASE WHEN title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, title LIMIT ?"
                )
            rows = conn.execute(sql, (pattern, pattern, pattern, limit)).fetchall()
        else:
            if include_deindexed:
                sql = (
                    "SELECT id, title, slug, category_id, builder_json, builder_public FROM pages "
                    "WHERE is_home=0 AND pending_deletion=0 AND title LIKE ? ESCAPE '\\' "
                    "ORDER BY title LIMIT ?"
                )
            else:
                sql = (
                    "SELECT id, title, slug, category_id, builder_json, builder_public FROM pages "
                    "WHERE is_home=0 AND pending_deletion=0 AND is_deindexed=0 AND title LIKE ? ESCAPE '\\' "
                    "ORDER BY title LIMIT ?"
                )
            rows = conn.execute(sql, (pattern, limit)).fetchall()
        return [dict(r) for r in rows]


def create_page(title, slug, content="", category_id=None, user_id=None,
                builder_json="", builder_public=False):
    """Create a new wiki page and record the initial history entry.  Returns the new page ID."""
    with get_db_context() as conn:
        system_authored = user_id is not None and str(user_id) == str(SYSTEM_USER_ID)
        try:
            if system_authored:
                conn.execute("PRAGMA foreign_keys=OFF")
            cur = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            # New pages go to the bottom: pick max sort_order + 1 within the same category scope
            max_row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM pages WHERE is_home=0 AND category_id IS ?"
                if category_id is None else
                "SELECT COALESCE(MAX(sort_order), -1) FROM pages WHERE is_home=0 AND category_id=?",
                (category_id,),
            ).fetchone()
            next_sort = (max_row[0] + 1) if max_row else 0
            cur.execute(
                "INSERT INTO pages (title, slug, content, builder_json, builder_public, category_id, last_edited_by, last_edited_at, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, slug, content, builder_json or "", 1 if builder_public else 0,
                 category_id, user_id, now, next_sort),
            )
            page_id = cur.lastrowid
            if user_id:
                cur.execute(
                    "INSERT INTO page_history (page_id, title, content, builder_json, builder_public, edited_by, edit_message) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (page_id, title, content, builder_json or "", 1 if builder_public else 0,
                     user_id, "Page created"),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if system_authored:
                conn.execute("PRAGMA foreign_keys=ON")
    return page_id


@retry_on_busy
def get_page(page_id):
    """Return the page row for the given *page_id*, or None if not found."""
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()


@retry_on_busy
def get_page_by_slug(slug):
    """Return the page row matching *slug*, or None if not found."""
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM pages WHERE slug=?", (slug,)).fetchone()


@retry_on_busy
def get_pages_in_category(cat_id):
    """Return all non-home page rows that belong to *cat_id*."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM pages WHERE category_id=? AND is_home=0", (cat_id,)
        ).fetchall()


@retry_on_busy
def list_all_pages():
    """Return every non-home wiki page row, newest first.

    Used by the admin bulk-management page to list pages for selection.
    Home pages are filtered out because they are not deletable through
    the standard delete flow, so exposing them in a bulk surface would
    create dead-end checkboxes.
    """
    with get_db_context() as conn:
        return conn.execute(
            "SELECT p.id, p.title, p.slug, p.category_id, p.is_deindexed, "
            "p.pending_deletion, c.name AS category_name "
            "FROM pages p LEFT JOIN categories c ON p.category_id = c.id "
            "WHERE p.is_home=0 ORDER BY p.id DESC"
        ).fetchall()


@retry_on_busy
def list_searchable_pages(include_deindexed=False):
    """Return pages with content/category metadata for advanced search."""
    deindex_clause = "" if include_deindexed else " AND p.is_deindexed=0"
    with get_db_context() as conn:
        return conn.execute(
            "SELECT p.id, p.title, p.slug, p.content, p.category_id, "
            "p.is_home, p.is_deindexed, p.pending_deletion, p.last_edited_at, "
            "p.builder_json, p.builder_public, "
            "c.name AS category_name "
            "FROM pages p LEFT JOIN categories c ON p.category_id = c.id "
            f"WHERE p.pending_deletion=0{deindex_clause} "
            "ORDER BY p.is_home DESC, p.title"
        ).fetchall()


@retry_on_busy
def list_active_pages_for_tts():
    """Return active wiki pages eligible for background TTS generation.

    Includes the home page and skips only deindexed pages plus pages
    queued for deletion. Used by the built-in ``tts`` plugin to backfill
    audio for every existing page when the admin enables the plugin.
    """
    with get_db_context() as conn:
        return conn.execute(
            "SELECT id, slug, title, content FROM pages "
            "WHERE is_deindexed=0 AND pending_deletion=0 "
            "ORDER BY id"
        ).fetchall()


@retry_on_busy
def get_home_page():
    """Return the designated home page row, or None if none has been set."""
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM pages WHERE is_home=1 ORDER BY id LIMIT 1").fetchone()


def set_home_page(page_id):
    """Set a page as the home page, unsetting any existing home page first.

    Args:
        page_id: The ID of the page to designate as home.

    Returns:
        The updated home page row.

    Raises:
        ValueError: If the page does not exist.
    """
    with get_db_context() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            page = conn.execute("SELECT id FROM pages WHERE id=?", (page_id,)).fetchone()
            if not page:
                conn.execute("ROLLBACK")
                raise ValueError("Page not found")
            conn.execute("UPDATE pages SET is_home=0 WHERE is_home=1 AND id != ?", (page_id,))
            conn.execute(
                "UPDATE pages SET is_home=1, is_deindexed=0, "
                "pending_deletion=0, pending_deletion_by=NULL, pending_deletion_at=NULL, "
                "protected_by=NULL, protected_at=NULL, "
                "protection_unlock_requested_at=NULL, protection_unlock_requested_by=NULL "
                "WHERE id=?",
                (page_id,),
            )
            conn.execute("DELETE FROM temp_pages WHERE page_id=?", (page_id,))
            conn.execute("DELETE FROM temp_page_index_state WHERE page_id=?", (page_id,))
            conn.execute("DELETE FROM page_reservations WHERE page_id=?", (page_id,))
            conn.execute("DELETE FROM user_page_cooldowns WHERE page_id=?", (page_id,))
            conn.commit()
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()


def update_page(page_id, title, content, user_id, edit_message="", is_revert=False,
                builder_json=None, builder_public=None,
                expected_last_edited_at=_NO_EXPECTED_REVISION):
    """Update a page and history atomically, optionally using revision CAS."""
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()
        current = conn.execute(
            "SELECT builder_json, builder_public, last_edited_at FROM pages WHERE id=?",
            (page_id,),
        ).fetchone()
        if expected_last_edited_at is not _NO_EXPECTED_REVISION:
            current_revision = (current["last_edited_at"] if current else None) or ""
            if current_revision != (expected_last_edited_at or ""):
                conn.rollback()
                return False
        if builder_json is None or builder_public is None:
            if builder_json is None:
                builder_json = current["builder_json"] if current else ""
            if builder_public is None:
                builder_public = current["builder_public"] if current else 0
        conn.execute(
            "UPDATE pages SET title=?, content=?, builder_json=?, builder_public=?, "
            "last_edited_by=?, last_edited_at=? WHERE id=?",
            (title, content, builder_json or "", 1 if builder_public else 0,
             user_id, now, page_id),
        )
        conn.execute(
            "INSERT INTO page_history (page_id, title, content, builder_json, builder_public, edited_by, edit_message, is_revert) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (page_id, title, content, builder_json or "", 1 if builder_public else 0,
             user_id, edit_message, 1 if is_revert else 0),
        )
        conn.commit()
        return True


def update_page_title(page_id, title, user_id):
    """Update only the title of a page, recording a history entry for the change."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        page = conn.execute(
            "SELECT title, content, builder_json, builder_public FROM pages WHERE id=?",
            (page_id,),
        ).fetchone()
        conn.execute("UPDATE pages SET title=?, last_edited_by=?, last_edited_at=? WHERE id=?",
                     (title, user_id, now, page_id))
        if page:
            conn.execute(
                "INSERT INTO page_history (page_id, title, content, builder_json, builder_public, edited_by, edit_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (page_id, title, page["content"], page["builder_json"], page["builder_public"],
                 user_id, f"Title changed from '{page['title']}' to '{title}'"),
            )
        conn.commit()


def update_page_category(page_id, category_id):
    """Move a page into the given category (or uncategorized if *category_id* is None)."""
    with get_db_context() as conn:
        page = conn.execute("SELECT is_home FROM pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            return False
        if page["is_home"]:
            return False
        conn.execute("UPDATE pages SET category_id=? WHERE id=?", (category_id, page_id))
        conn.commit()
        return True


VALID_DIFFICULTY_TAGS = ("", "beginner", "easy", "intermediate", "expert", "extra", "custom")
PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS = 72 * 3600


def set_page_protection(page_id, user_id):
    """Mark a page as protected by *user_id*."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        page = conn.execute("SELECT is_home FROM pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            return False
        if page["is_home"]:
            return False
        conn.execute(
            "UPDATE pages SET protected_by=?, protected_at=?, "
            "protection_unlock_requested_at=NULL, protection_unlock_requested_by=NULL "
            "WHERE id=?",
            (user_id, now, page_id),
        )
        conn.commit()
        return True


def clear_page_protection(page_id):
    """Remove protection and pending admin unlock data from a page."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE pages SET protected_by=NULL, protected_at=NULL, "
            "protection_unlock_requested_at=NULL, protection_unlock_requested_by=NULL "
            "WHERE id=?",
            (page_id,),
        )
        conn.commit()


def request_page_protection_unlock(page_id, requested_by):
    """Start or refresh the 72-hour admin unlock timer for a protected page."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE pages SET protection_unlock_requested_at=?, protection_unlock_requested_by=? "
            "WHERE id=?",
            (now, requested_by, page_id),
        )
        conn.commit()


@retry_on_busy
def list_protected_pages():
    """Return protected pages with protector and unlock-request metadata."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT p.id, p.slug, p.title, p.protected_by, p.protected_at, "
            "p.protection_unlock_requested_at, p.protection_unlock_requested_by, "
            "u.username AS protected_by_username, "
            "rq.username AS unlock_requested_by_username "
            "FROM pages p "
            "LEFT JOIN users u ON p.protected_by=u.id "
            "LEFT JOIN users rq ON p.protection_unlock_requested_by=rq.id "
            "WHERE p.protected_by IS NOT NULL "
            "ORDER BY p.title COLLATE NOCASE"
        ).fetchall()
    return rows


def update_page_tag(page_id, difficulty_tag, custom_label="", custom_color=""):
    """Set the difficulty tag for a page.

    *difficulty_tag* must be one of :data:`VALID_DIFFICULTY_TAGS`.  Custom label
    and color are only persisted when ``difficulty_tag == 'custom'``; they are
    cleared otherwise.
    """
    if difficulty_tag not in VALID_DIFFICULTY_TAGS:
        raise ValueError(f"Invalid difficulty tag: {difficulty_tag!r}")
    # Only persist custom fields when the custom tag type is chosen
    if difficulty_tag != "custom":
        custom_label = ""
        custom_color = ""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE pages SET difficulty_tag=?, tag_custom_label=?, tag_custom_color=? WHERE id=?",
            (difficulty_tag, custom_label, custom_color, page_id),
        )
        conn.commit()


def set_page_deindexed(page_id, is_deindexed):
    """Set or clear the deindexed flag for a page."""
    with get_db_context() as conn:
        page = conn.execute("SELECT is_home FROM pages WHERE id=?", (page_id,)).fetchone()
        if not page:
            return False
        if page["is_home"] and is_deindexed:
            return False
        conn.execute("UPDATE pages SET is_deindexed=? WHERE id=?", (1 if is_deindexed else 0, page_id))
        conn.commit()
        return True


def delete_page(page_id):
    """Delete a non-home page by ID.  Home pages are protected and cannot be deleted this way."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM pages WHERE id=? AND is_home=0", (page_id,))
        conn.commit()


@retry_on_busy
def get_page_history(page_id):
    """Return all history entries for a page, newest first, with editor usernames joined."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ph.*, "
            "CASE WHEN CAST(ph.edited_by AS TEXT) = '-1' THEN 'the system' "
            "     WHEN ph.edited_by IS NULL THEN '[removed]' "
            "     ELSE COALESCE(u.username, '[deleted user]') END AS username "
            "FROM page_history ph "
            "LEFT JOIN users u ON ph.edited_by=u.id "
            "WHERE ph.page_id=? ORDER BY ph.created_at DESC, ph.id DESC",
            (page_id,),
        ).fetchall()


@retry_on_busy
def get_history_entry(entry_id):
    """Return a single page history entry by its *entry_id*, with editor username joined."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ph.*, "
            "CASE WHEN CAST(ph.edited_by AS TEXT) = '-1' THEN 'the system' "
            "     WHEN ph.edited_by IS NULL THEN '[removed]' "
            "     ELSE COALESCE(u.username, '[deleted user]') END AS username "
            "FROM page_history ph LEFT JOIN users u ON ph.edited_by=u.id "
            "WHERE ph.id=?", (entry_id,)
        ).fetchone()


def transfer_history_attribution(entry_id, new_user_id):
    """Transfer a single page history entry's attribution to a different user."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE page_history SET edited_by=? WHERE id=?",
            (new_user_id, entry_id),
        )
        conn.commit()


def bulk_transfer_history_attribution(page_id, from_user_id, to_user_id):
    """Transfer all page history entries for a page from one user to another.

    Returns the number of entries updated.
    """
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE page_history SET edited_by=? WHERE page_id=? AND edited_by=?",
            (to_user_id, page_id, from_user_id),
        )
        count = cur.rowcount
        conn.commit()
        return count


def delete_history_entry(entry_id):
    """Delete a single page history entry by ID.

    Returns True if an entry was deleted, False if it did not exist.
    """
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM page_history WHERE id=?", (entry_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def clear_page_history(page_id):
    """Delete all history entries for a page.

    Returns the number of entries deleted.
    """
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM page_history WHERE page_id=?", (page_id,))
        count = cur.rowcount
        conn.commit()
        return count


_UPLOAD_REF_RE = re.compile(r'/static/uploads/([^\s)"\']+)')


@retry_on_busy
def get_all_referenced_image_filenames():
    """Return a set of upload filenames referenced in any content.

    Scans live page content, page history, unsaved drafts, announcements,
    and custom page content so that images still present anywhere in the
    application are never removed by the upload cleanup routine.
    """
    with get_db_context() as conn:
        filenames = set()
        for row in conn.execute("SELECT content FROM pages").fetchall():
            filenames.update(_UPLOAD_REF_RE.findall(row["content"]))
        for row in conn.execute("SELECT content FROM page_history").fetchall():
            filenames.update(_UPLOAD_REF_RE.findall(row["content"]))
        for row in conn.execute("SELECT content FROM drafts").fetchall():
            filenames.update(_UPLOAD_REF_RE.findall(row["content"]))
        for row in conn.execute("SELECT builder_json FROM page_builder_drafts").fetchall():
            filenames.update(_UPLOAD_REF_RE.findall(row["builder_json"]))
        for row in conn.execute("SELECT content FROM announcements WHERE content IS NOT NULL").fetchall():
            filenames.update(_UPLOAD_REF_RE.findall(row["content"]))
        # Custom pages may reference uploads in their content, CSS, or JS fields
        try:
            for row in conn.execute(
                "SELECT content, css, js FROM custom_pages"
            ).fetchall():
                for field in ("content", "css", "js"):
                    val = row[field]
                    if val:
                        filenames.update(_UPLOAD_REF_RE.findall(val))
        except sqlite3.OperationalError:
            # Table may not exist if the custom pages plugin is not installed
            pass
        return filenames


def add_page_attachment(page_id, filename, original_name, file_size, user_id, blob_id=None):
    """Record a new attachment for a page."""
    with get_db_context() as conn:
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO page_attachments (page_id, filename, original_name, file_size, uploaded_by, uploaded_at, blob_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (page_id, filename, original_name, file_size, user_id, now, blob_id),
        )
        attachment_id = cur.lastrowid
        conn.commit()
        return attachment_id


@retry_on_busy
def get_page_attachments(page_id):
    """Return all attachments for a page, ordered oldest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT pa.*, COALESCE(u.username, 'deleted user') AS uploader_name "
            "FROM page_attachments pa LEFT JOIN users u ON pa.uploaded_by=u.id "
            "WHERE pa.page_id=? ORDER BY pa.uploaded_at ASC",
            (page_id,),
        ).fetchall()


@retry_on_busy
def get_page_attachment(attachment_id):
    """Return a single attachment row by ID."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM page_attachments WHERE id=?", (attachment_id,)
        ).fetchone()


def delete_page_attachment(attachment_id):
    """Delete an attachment record and its associated blob."""
    with get_db_context() as conn:
        row = conn.execute("SELECT blob_id FROM page_attachments WHERE id=?", (attachment_id,)).fetchone()
        blob_id = row["blob_id"] if row else None
        conn.execute("DELETE FROM page_attachments WHERE id=?", (attachment_id,))
        if blob_id:
            conn.execute("DELETE FROM file_blobs WHERE id=?", (blob_id,))
        conn.commit()


#  @mention propagation
def propagate_mention_rename(old_username, new_username):
    """Rewrite @old_username → @new_username in all page and draft content.

    Called when a user changes their username so that existing @mentions
    reflect the new name automatically.  Only whole-word mentions are
    replaced (i.e. ``@alice`` won't corrupt ``@aliceinwonderland``).
    """
    pattern = re.compile(
        r"(?<!\w)@" + re.escape(old_username) + r"(?![A-Za-z0-9_-])"
    )
    old_fragment = f"@{old_username}"
    like_pat = f"%{_escape_like(old_fragment)}%"
    with get_db_context() as conn:
        pages = conn.execute(
            "SELECT id, content FROM pages WHERE content LIKE ? ESCAPE '\\'",
            (like_pat,),
        ).fetchall()
        for p in pages:
            updated = pattern.sub(f"@{new_username}", p["content"] or "")
            if updated != (p["content"] or ""):
                conn.execute("UPDATE pages SET content=?, last_edited_at=datetime('now') WHERE id=?", (updated, p["id"]))
        drafts = conn.execute(
            "SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'",
            (like_pat,),
        ).fetchall()
        for d in drafts:
            updated = pattern.sub(f"@{new_username}", d["content"] or "")
            if updated != (d["content"] or ""):
                conn.execute("UPDATE drafts SET content=?, updated_at=datetime('now') WHERE id=?", (updated, d["id"]))
        conn.commit()


def propagate_mention_deletion(username):
    """Replace @username with @account deleted in all page and draft content.

    Called when a user account is deleted so existing @mentions show a
    clear placeholder instead of a broken profile link.
    """
    pattern = re.compile(
        r"(?<!\w)@" + re.escape(username) + r"(?![A-Za-z0-9_-])"
    )
    old_fragment = f"@{username}"
    like_pat = f"%{_escape_like(old_fragment)}%"
    replacement = "@account deleted"
    with get_db_context() as conn:
        pages = conn.execute(
            "SELECT id, content FROM pages WHERE content LIKE ? ESCAPE '\\'",
            (like_pat,),
        ).fetchall()
        for p in pages:
            updated = pattern.sub(replacement, p["content"] or "")
            if updated != (p["content"] or ""):
                conn.execute("UPDATE pages SET content=?, last_edited_at=datetime('now') WHERE id=?", (updated, p["id"]))
        drafts = conn.execute(
            "SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'",
            (like_pat,),
        ).fetchall()
        for d in drafts:
            updated = pattern.sub(replacement, d["content"] or "")
            if updated != (d["content"] or ""):
                conn.execute("UPDATE drafts SET content=?, updated_at=datetime('now') WHERE id=?", (updated, d["id"]))
        conn.commit()
