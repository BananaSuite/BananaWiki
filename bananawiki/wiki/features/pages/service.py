"""Pages: the one place that reads visibility rules and writes page content.

Every caller (web editor, REST API, imports, federation, page builder,
background jobs) goes through these functions, so history, search, events and
permission rules behave the same everywhere.

Visibility
----------
A page is visible to a user when they can read its category and:

* it is not pending deletion, unless they may delete pages;
* it is not hidden (``is_deindexed``), unless they may see hidden pages;
* anonymous visitors (public mode) additionally never see builder pages that
  are not marked public.

Editing
-------
Editors may change pages in categories they can write to. Plain users never
edit directly (they propose contributions, see the governance feature).
``update`` takes the revision the editor started from and raises
:class:`EditConflict` when someone saved in between.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from flask import current_app

from ....core.timeutil import now_sql
from ... import auth, settings
from ...db import db
from ...registry import emit

MAX_TITLE = 200
MAX_CONTENT = 1_000_000
MAX_EDIT_MESSAGE = 500
PAGE_COLUMNS = (
    "id, title, slug, category_id, is_home, sort_order, is_deindexed, pending_deletion, "
    "pending_deletion_by, pending_deletion_at, protected_by, protected_at, difficulty_tag, "
    "tag_custom_label, tag_custom_color, last_edited_by, last_edited_at, created_at, revision, builder_public, "
    "CASE WHEN builder_json != '' THEN 1 ELSE 0 END AS has_builder"
)


class PageError(ValueError):
    """A refused page change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


class EditConflict(PageError):
    def __init__(self, current: dict[str, Any]):
        super().__init__("wiki.error.edit_conflict")
        self.current = current


@dataclass(frozen=True)
class Draft:
    title: str
    content: str


# ── Slugs ─────────────────────────────────────────────────────────────────────

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACES = re.compile(r"[\s_]+")
RESERVED_SLUGS = frozenset({"new", "edit", "history", "search", "api", "admin", "static"})


def slugify(text: str) -> str:
    """URL slug of a title (same rules as 1.4, so existing links stay valid)."""
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    text = _SLUG_STRIP.sub("", text)
    text = _SLUG_SPACES.sub("-", text).strip("-")
    return text[:180] or "page"


def unique_slug(base: str, *, exclude_page_id: int | None = None) -> str:
    base = slugify(base)
    if base in RESERVED_SLUGS:
        base = f"{base}-page"
    candidate, n = base, 2
    while True:
        row = db.one("SELECT id FROM pages WHERE slug = ?", (candidate,))
        if row is None or row["id"] == exclude_page_id:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


# ── Reading ──────────────────────────────────────────────────────────────────


def get(page_id: int | str | None, *, with_content: bool = True) -> dict[str, Any] | None:
    if page_id in (None, ""):
        return None
    try:
        page_id = int(page_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    columns = "*" if with_content else PAGE_COLUMNS
    return db.one(f"SELECT {columns} FROM pages WHERE id = ?", (page_id,))


def get_by_slug(slug: str | None, *, with_content: bool = True) -> dict[str, Any] | None:
    if not slug:
        return None
    columns = "*" if with_content else PAGE_COLUMNS
    return db.one(f"SELECT {columns} FROM pages WHERE slug = ?", (slug,))


def home() -> dict[str, Any] | None:
    return db.one("SELECT * FROM pages WHERE is_home = 1")


def _flag(page: dict[str, Any], key: str) -> bool:
    return bool(page.get(key))


def can_view(page: dict[str, Any] | None, user: dict[str, Any] | None = None, *, anonymous: bool = False) -> bool:
    """Whether *user* (default: the current user) may read *page*."""
    if page is None:
        return False
    if not anonymous and user is None:
        user = auth.current_user()
    if user is None:
        if not settings.public_mode_active():
            return False
        if _flag(page, "pending_deletion") or _flag(page, "is_deindexed"):
            return False
        has_builder = page.get("has_builder") if "has_builder" in page else bool(page.get("builder_json"))
        if has_builder and (not page.get("builder_public") or current_app.config["BW"].forbid_public_builder_pages):
            return False
        return True
    if auth.is_admin(user):
        return True
    if not auth.can_read_category(page.get("category_id"), user):
        return False
    if _flag(page, "pending_deletion") and not auth.has_permission("page.delete", user):
        return False
    return not (_flag(page, "is_deindexed") and not auth.has_permission("page.view_deindexed", user))


def can_edit(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    if not user or not can_view(page, user):
        return False
    if auth.is_admin(user):
        return True
    return (auth.has_role("editor", user) and auth.has_permission("page.edit_all", user)
            and auth.can_write_category(page.get("category_id"), user))


def can_create(category_id: int | None, user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    if not user:
        return False
    if auth.is_admin(user):
        return True
    return (auth.has_role("editor", user) and auth.has_permission("page.create", user)
            and auth.can_write_category(category_id, user))


def can_delete(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    if not user or page.get("is_home"):
        return False
    if auth.is_admin(user):
        return True
    return can_edit(page, user) and auth.has_permission("page.delete", user)


def visible_filter(user: dict[str, Any] | None = None, alias: str = "p") -> tuple[str, list[Any]]:
    """SQL condition (and params) selecting the pages *user* may see.

    Category restrictions are applied in SQL so listings and search never
    load pages the reader cannot open.
    """
    user = auth.current_user() if user is None else user
    a = alias
    if user is None:
        if not settings.public_mode_active():
            return "0", []
        clause = f"{a}.pending_deletion = 0 AND {a}.is_deindexed = 0"
        if current_app.config["BW"].forbid_public_builder_pages:
            clause += f" AND {a}.builder_json = ''"
        else:
            clause += f" AND ({a}.builder_json = '' OR {a}.builder_public = 1)"
        return clause, []
    if auth.is_admin(user):
        return "1", []
    grants = auth.grants(user)
    assert grants is not None
    parts: list[str] = []
    params: list[Any] = []
    if not auth.has_permission("page.delete", user):
        parts.append(f"{a}.pending_deletion = 0")
    if not auth.has_permission("page.view_deindexed", user):
        parts.append(f"{a}.is_deindexed = 0")
    readable: set[int] | None = None
    if grants.read.restricted:
        readable = set(grants.read.allowed)
        if grants.role == "editor" and grants.write.restricted:
            readable |= set(grants.write.allowed)
        elif grants.role == "editor" and not grants.write.restricted:
            readable = None
    if readable is not None:
        if readable:
            marks = ",".join("?" for _ in readable)
            parts.append(f"{a}.category_id IN ({marks})")
            params.extend(sorted(readable))
        else:
            parts.append("0")
    return " AND ".join(parts) or "1", params


def list_visible(
    *, category_id: int | None | str = "any", user: dict[str, Any] | None = None, include_home: bool = True,
    order: str = "title", limit: int | None = None, offset: int = 0,
) -> list[dict[str, Any]]:
    """Pages the user may see; ``category_id=None`` lists uncategorised pages."""
    where, params = visible_filter(user)
    if category_id is None:
        where += " AND p.category_id IS NULL"
    elif category_id != "any":
        where += " AND p.category_id = ?"
        params.append(int(category_id))
    if not include_home:
        where += " AND p.is_home = 0"
    ordering = {
        "title": "p.title COLLATE NOCASE, p.id",
        "sort": "p.sort_order, p.title COLLATE NOCASE, p.id",
        "recent": "COALESCE(p.last_edited_at, p.created_at) DESC, p.id DESC",
        "created": "p.created_at DESC, p.id DESC",
    }[order]
    sql = f"SELECT {_prefixed(PAGE_COLUMNS)} FROM pages p WHERE {where} ORDER BY {ordering}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    return db.all(sql, params)


def _prefixed(columns: str) -> str:
    out = []
    for column in columns.split(", "):
        out.append(column if column.startswith("CASE") else f"p.{column}")
    return ", ".join(out).replace("WHEN builder_json", "WHEN p.builder_json")


def adjacent(page: dict[str, Any], user: dict[str, Any] | None = None) -> tuple[dict | None, dict | None]:
    """Previous and next page for categories with sequential navigation."""
    if not page.get("category_id"):
        return None, None
    category = db.one("SELECT sequential_nav FROM categories WHERE id = ?", (page["category_id"],))
    if not category or not category["sequential_nav"]:
        return None, None
    where, params = visible_filter(user)
    base = (f"SELECT p.id, p.title, p.slug FROM pages p WHERE {where} AND p.category_id = ? AND p.is_home = 0 "
            f"AND p.is_deindexed = 0 AND p.pending_deletion = 0 AND p.id != ?")
    key = (page.get("sort_order") or 0, page["title"].lower(), page["id"])
    previous = db.one(
        base + " AND (p.sort_order < ? OR (p.sort_order = ? AND (lower(p.title) < ? OR "
        "(lower(p.title) = ? AND p.id < ?)))) ORDER BY p.sort_order DESC, lower(p.title) DESC, p.id DESC LIMIT 1",
        [*params, page["category_id"], page["id"], key[0], key[0], key[1], key[1], key[2]],
    )
    following = db.one(
        base + " AND (p.sort_order > ? OR (p.sort_order = ? AND (lower(p.title) > ? OR "
        "(lower(p.title) = ? AND p.id > ?)))) ORDER BY p.sort_order, lower(p.title), p.id LIMIT 1",
        [*params, page["category_id"], page["id"], key[0], key[0], key[1], key[1], key[2]],
    )
    return previous, following


# ── Writing ──────────────────────────────────────────────────────────────────


def _clean_title(title: str | None) -> str:
    title = " ".join((title or "").split())
    if not title:
        raise PageError("wiki.error.title_required")
    if len(title) > MAX_TITLE:
        raise PageError("wiki.error.title_too_long", limit=MAX_TITLE)
    return title


def _clean_content(content: str | None) -> str:
    content = (content or "").replace("\r\n", "\n")
    if len(content) > MAX_CONTENT:
        raise PageError("wiki.error.content_too_long")
    return content


def _check_category(category_id: int | None) -> int | None:
    if category_id in (None, "", 0, "0"):
        return None
    try:
        category_id = int(category_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise PageError("wiki.error.category_missing") from None
    if not db.scalar("SELECT 1 FROM categories WHERE id = ?", (category_id,)):
        raise PageError("wiki.error.category_missing")
    return category_id


def _record_history(page_id: int, title: str, content: str, author_id: str | None, message: str,
                    *, builder_json: str = "", builder_public: int = 0, is_revert: bool = False) -> int:
    return db.insert("page_history", {
        "page_id": page_id,
        "title": title,
        "content": content,
        "builder_json": builder_json or "",
        "builder_public": builder_public,
        "edited_by": author_id,
        "edit_message": (message or "")[:MAX_EDIT_MESSAGE],
        "is_revert": 1 if is_revert else 0,
        "created_at": now_sql(),
    })


def create(
    title: str,
    content: str = "",
    *,
    category_id: int | None = None,
    author_id: str | None,
    edit_message: str = "",
    slug: str | None = None,
    builder_json: str = "",
    builder_public: bool = False,
    sort_order: int | None = None,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Create a page and its first history entry. *author_id* None means "system"."""
    title = _clean_title(title)
    content = _clean_content(content)
    with db.transaction():
        category_id = _check_category(category_id)
        final_slug = unique_slug(slug or title)
        if sort_order is None:
            sort_order = int(db.scalar(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM pages WHERE category_id IS ?", (category_id,),
                default=0,
            ))
        now = now_sql()
        page_id = db.insert("pages", {
            "title": title, "slug": final_slug, "content": content, "category_id": category_id,
            "builder_json": builder_json or "", "builder_public": 1 if builder_public else 0,
            "sort_order": sort_order, "last_edited_by": author_id, "last_edited_at": now, "created_at": now,
            "revision": 1,
        })
        _record_history(page_id, title, content, author_id, edit_message or "Created",
                        builder_json=builder_json, builder_public=1 if builder_public else 0)
    page = get(page_id)
    assert page is not None
    if emit_event:
        emit("page.created", page=page, author_id=author_id)
    return page


def update(
    page: dict[str, Any],
    *,
    author_id: str | None,
    title: str | None = None,
    content: str | None = None,
    edit_message: str = "",
    expected_revision: int | None = None,
    builder_json: str | None = None,
    builder_public: bool | None = None,
    is_revert: bool = False,
    record_history: bool = True,
) -> dict[str, Any]:
    """Save new title/content. Raises :class:`EditConflict` on a stale *expected_revision*."""
    new_title = _clean_title(title) if title is not None else page["title"]
    new_content = _clean_content(content) if content is not None else page["content"]
    new_builder = builder_json if builder_json is not None else page.get("builder_json") or ""
    new_public = (1 if builder_public else 0) if builder_public is not None else page.get("builder_public") or 0
    with db.transaction():
        current = get(page["id"])
        if current is None:
            raise PageError("wiki.error.page_missing")
        if expected_revision is not None and int(current.get("revision") or 0) != int(expected_revision):
            raise EditConflict(current)
        unchanged = (new_title == current["title"] and new_content == current["content"]
                     and new_builder == (current.get("builder_json") or "")
                     and new_public == (current.get("builder_public") or 0))
        if unchanged:
            return current
        now = now_sql()
        db.execute(
            "UPDATE pages SET title = ?, content = ?, builder_json = ?, builder_public = ?, last_edited_by = ?, "
            "last_edited_at = ?, revision = revision + 1 WHERE id = ?",
            (new_title, new_content, new_builder, new_public, author_id, now, page["id"]),
        )
        if record_history:
            _record_history(page["id"], new_title, new_content, author_id, edit_message,
                            builder_json=new_builder, builder_public=new_public, is_revert=is_revert)
    updated = get(page["id"])
    assert updated is not None
    emit("page.updated", page=updated, previous=current, author_id=author_id)
    return updated


def set_fields(page_id: int, **fields: Any) -> None:
    """Change page metadata that is not versioned (tags, flags, order)."""
    allowed = {"is_deindexed", "difficulty_tag", "tag_custom_label", "tag_custom_color", "sort_order",
               "protected_by", "protected_at", "protection_unlock_requested_at",
               "protection_unlock_requested_by", "pending_deletion", "pending_deletion_by", "pending_deletion_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise KeyError(f"Not a page metadata field: {sorted(unknown)}")
    db.update("pages", fields, "id = ?", (page_id,))


def move(page: dict[str, Any], category_id: int | None, *, actor_id: str | None) -> dict[str, Any]:
    with db.transaction():
        category_id = _check_category(category_id)
        if category_id == page.get("category_id"):
            return page
        order = int(db.scalar(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM pages WHERE category_id IS ?", (category_id,), default=0
        ))
        db.execute("UPDATE pages SET category_id = ?, sort_order = ? WHERE id = ?", (category_id, order, page["id"]))
    moved = get(page["id"])
    assert moved is not None
    emit("page.moved", page=moved, previous_category_id=page.get("category_id"), actor_id=actor_id)
    return moved


def change_slug(page: dict[str, Any], new_slug: str, *, actor_id: str | None = None) -> dict[str, Any]:
    """Give *page* a new address and rewrite ``/page/<old>`` links in other pages and drafts.

    Each rewritten page gets a history entry attributed to *actor_id*.
    """
    slug = slugify(new_slug)
    if slug in RESERVED_SLUGS:
        raise PageError("wiki.error.slug_reserved")
    if slug == page["slug"]:
        return page
    with db.transaction():
        if db.scalar("SELECT 1 FROM pages WHERE slug = ? AND id != ?", (slug, page["id"])):
            raise PageError("wiki.error.slug_taken")
        db.execute("UPDATE pages SET slug = ? WHERE id = ?", (slug, page["id"]))
    changed = get(page["id"])
    assert changed is not None
    _rewrite_links(page["slug"], slug, exclude_page_id=page["id"], actor_id=actor_id)
    emit("page.renamed", page=changed, old_slug=page["slug"])
    return changed


def _like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _rewrite_links(old_slug: str, new_slug: str, *, exclude_page_id: int, actor_id: str | None) -> None:
    old_link, new_link = f"/page/{old_slug}", f"/page/{new_slug}"
    # "/page/notes" must not match "/page/notes-archive".
    pattern = re.compile(re.escape(old_link) + r"(?![\w-])")
    like = _like_pattern(old_link)
    for row in db.all("SELECT id FROM pages WHERE id != ? AND content LIKE ? ESCAPE '\\'", (exclude_page_id, like)):
        other = get(row["id"])
        if other is None:
            continue
        rewritten = pattern.sub(new_link, other["content"])
        if rewritten != other["content"]:
            update(other, author_id=actor_id, content=rewritten,
                   edit_message=f"Updated links to /page/{new_slug}")
    for draft in db.all("SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'", (like,)):
        rewritten = pattern.sub(new_link, draft["content"])
        if rewritten != draft["content"]:
            db.execute("UPDATE drafts SET content = ? WHERE id = ?", (rewritten, draft["id"]))


def set_home(page: dict[str, Any]) -> None:
    with db.transaction():
        db.execute("UPDATE pages SET is_home = 0 WHERE is_home = 1 AND id != ?", (page["id"],))
        db.execute(
            "UPDATE pages SET is_home = 1, is_deindexed = 0, pending_deletion = 0, pending_deletion_by = NULL, "
            "pending_deletion_at = NULL WHERE id = ?",
            (page["id"],),
        )


def delete(page: dict[str, Any], *, actor_id: str | None) -> None:
    """Delete a page permanently (history and attachments go with it)."""
    if page.get("is_home"):
        raise PageError("wiki.error.cannot_delete_home")
    attachments = db.all("SELECT filename FROM page_attachments WHERE page_id = ?", (page["id"],))
    with db.transaction():
        db.execute("DELETE FROM pages WHERE id = ?", (page["id"],))
    from ... import storage

    for row in attachments:
        storage.delete("attachments", row["filename"])
    emit("page.deleted", page=page, actor_id=actor_id)


def revert(page: dict[str, Any], history_id: int, *, author_id: str | None) -> dict[str, Any]:
    entry = db.one("SELECT * FROM page_history WHERE id = ? AND page_id = ?", (history_id, page["id"]))
    if entry is None:
        raise PageError("wiki.error.revision_missing")
    return update(
        page, author_id=author_id, title=entry["title"], content=entry["content"],
        builder_json=entry.get("builder_json") or "", builder_public=bool(entry.get("builder_public")),
        edit_message=f"Reverted to revision {history_id}", is_revert=True,
    )


def history(page_id: int, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    return db.all(
        "SELECT h.id, h.page_id, h.title, h.edited_by, h.edit_message, h.is_revert, h.created_at, "
        "length(h.content) AS size, u.username AS editor "
        "FROM page_history h LEFT JOIN users u ON u.id = h.edited_by "
        "WHERE h.page_id = ? ORDER BY h.id DESC LIMIT ? OFFSET ?",
        (page_id, limit, offset),
    )


def history_entry(entry_id: int) -> dict[str, Any] | None:
    return db.one(
        "SELECT h.*, u.username AS editor FROM page_history h LEFT JOIN users u ON u.id = h.edited_by WHERE h.id = ?",
        (entry_id,),
    )


def delete_history_entry(page_id: int, entry_id: int) -> bool:
    """Remove one history entry of *page_id*; the page itself is unchanged."""
    return db.execute("DELETE FROM page_history WHERE id = ? AND page_id = ?", (entry_id, page_id)).rowcount > 0


def clear_history(page_id: int) -> int:
    return db.execute("DELETE FROM page_history WHERE page_id = ?", (page_id,)).rowcount


def transfer_history(page_id: int, entry_id: int, user_id: str | None) -> bool:
    """Credit one entry to *user_id* (``None`` removes the attribution)."""
    return db.execute(
        "UPDATE page_history SET edited_by = ? WHERE id = ? AND page_id = ?", (user_id, entry_id, page_id)
    ).rowcount > 0


def transfer_history_bulk(page_id: int, from_user_id: str, to_user_id: str) -> int:
    """Credit every entry of *page_id* made by *from_user_id* to *to_user_id*."""
    return db.execute(
        "UPDATE page_history SET edited_by = ? WHERE page_id = ? AND edited_by = ?",
        (to_user_id, page_id, from_user_id),
    ).rowcount


# ── Mentions ─────────────────────────────────────────────────────────────────


def _mention_pattern(username: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w@/])@" + re.escape(username) + r"(?![A-Za-z0-9_-])")


def rewrite_mentions(old_username: str, replacement: str) -> int:
    """Replace ``@old_username`` in pages and drafts, recording a history entry per page."""
    pattern = _mention_pattern(old_username)
    like = "%@" + old_username.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    changed = 0
    for row in db.all("SELECT id FROM pages WHERE content LIKE ? ESCAPE '\\'", (like,)):
        page = get(row["id"])
        if page is None:
            continue
        new_content = pattern.sub(replacement, page["content"])
        if new_content != page["content"]:
            update(page, author_id=None, content=new_content,
                   edit_message=f"Updated mention of @{old_username}")
            changed += 1
    for draft in db.all("SELECT id, content FROM drafts WHERE content LIKE ? ESCAPE '\\'", (like,)):
        new_content = pattern.sub(replacement, draft["content"])
        if new_content != draft["content"]:
            db.execute("UPDATE drafts SET content = ? WHERE id = ?", (new_content, draft["id"]))
    return changed


# ── Search ───────────────────────────────────────────────────────────────────

_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_available() -> bool:
    """Whether the ``pages_fts`` full-text index exists (SQLite built with FTS5)."""
    cache = current_app.extensions.get("bananawiki.fts")
    if cache is None:
        cache = bool(db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'pages_fts'"))
        current_app.extensions["bananawiki.fts"] = cache
    return cache


def search(query: str, *, user: dict[str, Any] | None = None, limit: int = 20, offset: int = 0,
           titles_only: bool = False) -> list[dict[str, Any]]:
    """Full-text search over the pages *user* may see, best matches first."""
    query = (query or "").strip()[:200]
    if not query:
        return []
    where, params = visible_filter(user)
    tokens = _FTS_TOKEN.findall(query)
    if fts_available() and tokens:
        columns = "{title}" if titles_only else "{title content}"
        match = " AND ".join(f'{columns} : "{token}"*' for token in tokens[:12])
        return db.all(
            f"SELECT p.id, p.title, p.slug, p.category_id, p.last_edited_at, "
            f"snippet(pages_fts, 1, '\x02', '\x03', ' … ', 14) AS snippet, bm25(pages_fts, 8.0, 1.0) AS score "
            f"FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
            f"WHERE pages_fts MATCH ? AND {where} ORDER BY score LIMIT ? OFFSET ?",
            [match, *params, limit, offset],
        )
    like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    field = "p.title LIKE ? ESCAPE '\\'" if titles_only else "(p.title LIKE ? ESCAPE '\\' OR p.content LIKE ? ESCAPE '\\')"
    like_params = [like] if titles_only else [like, like]
    return db.all(
        f"SELECT p.id, p.title, p.slug, p.category_id, p.last_edited_at, substr(p.content, 1, 240) AS snippet, "
        f"0 AS score FROM pages p WHERE {field} AND {where} "
        f"ORDER BY CASE WHEN p.title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, p.title COLLATE NOCASE LIMIT ? OFFSET ?",
        [*like_params, *params, like, limit, offset],
    )
