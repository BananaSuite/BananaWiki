"""Pages: the one place that reads visibility rules and writes page content.

Every caller (web editor, REST API, imports, federation, page builder,
background jobs) goes through these functions, so history, search, events and
permission rules behave the same everywhere.

Visibility
----------
A page is visible to a user when they can read its category and:

* signed-in users hold ``page.view_all`` (administrators always do);
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

import itertools
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from flask import current_app
from werkzeug.exceptions import ServiceUnavailable

from ....core.timeutil import now_sql
from ... import auth, settings
from ...db import db
from ...i18n import t
from ...registry import emit

MAX_TITLE = 200
MAX_CONTENT = 1_000_000
MAX_EDIT_MESSAGE = 500
REWRITE_ATTEMPTS = 3  # reads of a page whose links or mentions change while others save it
# Characters of linking pages one change of address saves as new history versions. The pages
# past it get the new links without a version of their own: a rename cannot copy every page
# that links to it into the history.
MAX_REWRITE_HISTORY_CHARS = 4_000_000
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


# Slugs

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


# Reading


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
    if not auth.has_permission("page.view_all", user):
        return False
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
    if not auth.has_permission("page.view_all", user):
        return "0", []
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


# Writing


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
    """Save new title/content. Raises :class:`EditConflict` on a stale *expected_revision*.

    Fields left as ``None`` keep their stored value, read in the same
    transaction: *page* may be older than a save that happened meanwhile.
    """
    cleaned_title = _clean_title(title) if title is not None else None
    cleaned_content = _clean_content(content) if content is not None else None
    with db.transaction():
        current = get(page["id"])
        if current is None:
            raise PageError("wiki.error.page_missing")
        if expected_revision is not None and int(current.get("revision") or 0) != int(expected_revision):
            raise EditConflict(current)
        new_title = current["title"] if cleaned_title is None else cleaned_title
        new_content = current["content"] if cleaned_content is None else cleaned_content
        new_builder = (current.get("builder_json") or "") if builder_json is None else builder_json
        new_public = (current.get("builder_public") or 0) if builder_public is None else (1 if builder_public else 0)
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

    Each rewritten page gets a history entry attributed to *actor_id*, until
    those entries reach :data:`MAX_REWRITE_HISTORY_CHARS`; later pages are
    rewritten without one. Only pages the actor may edit right now are
    rewritten (with their drafts): a page they cannot read or write, or that
    is protected, checked out or pending deletion, keeps the old links, as
    does a page that cannot take the new ones (it is logged). The rename
    stands and ``page.renamed`` is emitted all the same. Without *actor_id*
    (the system) every page is rewritten.
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
    try:
        _rewrite_links(page["slug"], slug, exclude_page_id=page["id"], actor_id=actor_id)
    finally:
        emit("page.renamed", page=changed, old_slug=page["slug"])
    return changed


def _like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _actor(user_id: str | None) -> dict[str, Any] | None:
    """The account behind *user_id* (the signed-in one without a query); None for the system."""
    if not user_id:
        return None
    current = auth.current_user()
    if current is not None and current["id"] == user_id:
        return current
    return db.one("SELECT * FROM users WHERE id = ?", (str(user_id),))


def _may_rewrite(page_id: int, actor: dict[str, Any] | None) -> bool:
    """Whether a rename by *actor* may change the links in page *page_id* (always for the system)."""
    if actor is None:
        return True
    from .access import edit_blocked  # access imports this module

    page = get(page_id, with_content=False)
    return page is not None and can_edit(page, actor) and not edit_blocked(page, actor)


def _rewrite_links(old_slug: str, new_slug: str, *, exclude_page_id: int, actor_id: str | None) -> None:
    old_link, new_link = f"/page/{old_slug}", f"/page/{new_slug}"
    # "/page/notes" must not match "/page/notes-archive".
    pattern = re.compile(re.escape(old_link) + r"(?![\w-])")

    def change(text: str) -> str:
        return pattern.sub(new_link, text)

    like = _like_pattern(old_link)
    actor = _actor(actor_id)
    history_left = MAX_REWRITE_HISTORY_CHARS
    # A builder page's Markdown twin shows every link of its document, so the content finds those too.
    for row in db.all("SELECT id, length(content) + length(builder_json) AS size FROM pages "
                      "WHERE id != ? AND content LIKE ? ESCAPE '\\' ORDER BY id", (exclude_page_id, like)):
        if not _may_rewrite(row["id"], actor):
            # Someone who may not edit that page does not change it, nor appear in its history.
            current_app.logger.info("Links to /page/%s left in page %s: not editable by the renamer",
                                    old_slug, row["id"])
            continue
        record = row["size"] <= history_left
        if record:
            history_left -= row["size"]
        try:
            _rewrite_page(row["id"], change, author_id=actor_id, edit_message=f"Updated links to /page/{new_slug}",
                          renamed=(old_slug, new_slug), record_history=record)
        except PageError as error:
            current_app.logger.warning("Links to /page/%s left in page %s: %s", old_slug, row["id"], error)
    for draft in db.all("SELECT id, page_id FROM drafts WHERE content LIKE ? ESCAPE '\\' ORDER BY id", (like,)):
        if _may_rewrite(draft["page_id"], actor):
            _rewrite_draft(draft["id"], change)


def _rewrite_draft(draft_id: int, change: Callable[[str], str]) -> None:
    """Apply *change* to the text of draft *draft_id*.

    Drafts are read one at a time: the matching drafts of every account
    together can hold far more text than a worker should keep at once.
    """
    content = db.scalar("SELECT content FROM drafts WHERE id = ?", (draft_id,))
    if content is None:
        return
    rewritten = change(content)
    if rewritten != content:
        db.execute("UPDATE drafts SET content = ? WHERE id = ?", (rewritten, draft_id))


def _rewrite_page(page_id: int, change: Callable[[str], str], *, author_id: str | None, edit_message: str,
                  renamed: tuple[str, str] | None = None, record_history: bool = True) -> bool:
    """Save page *page_id* with *change* applied to its text; whether it changed.

    A builder page whose body comes from its document gets the change in the
    document and a matching Markdown twin, whether or not the builder is
    switched on, so it keeps rendering from it. A save by someone else in
    between means reading the page again; a page that cannot take the change
    raises :class:`PageError` and stays as it is.
    """
    for attempt in range(1, REWRITE_ATTEMPTS + 1):
        page = get(page_id)
        if page is None:
            return False
        values = _changed_text(page, change, renamed)
        if values is None:
            return False
        try:
            update(page, author_id=author_id, edit_message=edit_message, expected_revision=page["revision"],
                   record_history=record_history, **values)
        except EditConflict:
            if attempt == REWRITE_ATTEMPTS:
                raise
            continue
        return True
    return False


def _changed_text(page: dict[str, Any], change: Callable[[str], str],
                  renamed: tuple[str, str] | None) -> dict[str, str] | None:
    """The ``content`` (and ``builder_json``) of *page* after *change*, or None when nothing changes."""
    values: dict[str, str] | None = None
    if page.get("builder_json"):
        # The document is stored with the page: kept in step here, not by a handler of the
        # feature, which would not run while the builder is switched off.
        from ..page_builder import document

        try:
            values = document.rewrite(page, change, renamed=renamed)
        except document.DocumentError as error:
            raise PageError(error.key) from None
    if values is None:
        values = {"content": change(page["content"])}
    if all(value == (page.get(key) or "") for key, value in values.items()):
        return None
    return values


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


def may_publish_public(user: dict[str, Any]) -> bool:
    """Only administrators may expose a builder page to public-mode visitors, if the host allows it."""
    return auth.is_admin(user) and not current_app.config["BW"].forbid_public_builder_pages


def revert(page: dict[str, Any], history_id: int, *, author_id: str | None) -> dict[str, Any]:
    """Save history entry *history_id* again as the page's newest version, on behalf of *author_id*.

    The title comes back only for someone who may change it
    (``page.edit_metadata``); otherwise the page keeps its current title. As
    when publishing in the page builder, a builder page stays public only when
    an administrator restores a public version: a revert by anyone else needs
    a fresh approval.
    """
    entry = db.one("SELECT * FROM page_history WHERE id = ? AND page_id = ?", (history_id, page["id"]))
    if entry is None:
        raise PageError("wiki.error.revision_missing")
    from .access import can_edit_metadata  # access imports this module

    actor = _actor(author_id)
    return update(
        page, author_id=author_id, title=entry["title"] if actor is None or can_edit_metadata(page, actor) else None,
        content=entry["content"],
        builder_json=entry.get("builder_json") or "",
        builder_public=bool(entry.get("builder_public")) and (actor is None or may_publish_public(actor)),
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


# Mentions


def _mention_pattern(username: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w@/])@" + re.escape(username) + r"(?![A-Za-z0-9_-])")


def rewrite_mentions(old_username: str, replacement: str) -> int:
    """Replace ``@old_username`` in pages and drafts, recording a history entry per page.

    Used by account merges, an administrator's decision; renaming or deleting an
    account leaves pages alone. A page edited meanwhile is read again; one that
    cannot take the change (over a size limit with a longer name) keeps its text
    instead of stopping the merge half-way.
    """
    pattern = _mention_pattern(old_username)

    def change(text: str) -> str:
        return pattern.sub(replacement, text)

    like = "%@" + old_username.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    changed = 0
    for row in db.all("SELECT id FROM pages WHERE content LIKE ? ESCAPE '\\'", (like,)):
        try:
            if _rewrite_page(row["id"], change, author_id=None, edit_message=f"Updated mention of @{old_username}"):
                changed += 1
        except PageError as error:
            current_app.logger.warning("Mention of @%s left in page %s: %s", old_username, row["id"], error)
    for draft_id in db.column("SELECT id FROM drafts WHERE content LIKE ? ESCAPE '\\' ORDER BY id", (like,)):
        _rewrite_draft(draft_id, change)
    return changed


# Search

# The tokenizer of ``pages_fts`` (migrations/v4_takeover.py). Search words are split by SQLite itself, not a
# regular expression: unicode61 knows Unicode 6.1, so some characters Python counts as letters are separators to it.
FTS_TOKENIZE = "unicode61 remove_diacritics 2"
_tokenizer_lock = threading.Lock()
_tokenizer: sqlite3.Connection | None = None


def fts_words(text: str) -> list[str]:
    """The words of *text* exactly as the full-text index sees them (case and diacritics folded)."""
    global _tokenizer
    with _tokenizer_lock:
        if _tokenizer is None:
            conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            conn.execute(f"CREATE VIRTUAL TABLE words USING fts5(text, tokenize='{FTS_TOKENIZE}')")
            conn.execute("CREATE VIRTUAL TABLE words_vocab USING fts5vocab(words, 'instance')")
            _tokenizer = conn
        _tokenizer.execute("BEGIN")
        try:
            _tokenizer.execute("INSERT INTO words (rowid, text) VALUES (1, ?)", (text,))
            return [row[0] for row in _tokenizer.execute("SELECT term FROM words_vocab ORDER BY offset")]
        finally:
            _tokenizer.execute("ROLLBACK")


def fts_available() -> bool:
    """Whether the ``pages_fts`` full-text index exists (SQLite built with FTS5)."""
    cache = current_app.extensions.get("bananawiki.fts")
    if cache is None:
        cache = bool(db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'pages_fts'"))
        current_app.extensions["bananawiki.fts"] = cache
    return cache


# Shorter words are matched whole: as a prefix, one or two letters stand for nearly every word in the index.
MIN_PREFIX = 3


def fts_phrase(words: list[str]) -> str:
    """An FTS5 phrase for *words*, the last one also matching longer words when it has MIN_PREFIX characters.

    The words are split again by the index's own tokenizer, so padding (``a__``, or letters newer than the
    tokenizer's Unicode 6.1 such as ``a`` + U+19B0 U+19B0) cannot make a short word long.
    """
    words = fts_words(" ".join(words))
    return '"' + " ".join(words) + '"' + ("*" if words and len(words[-1]) >= MIN_PREFIX else "")


# A backstop: a search statement still running after this long is stopped (SQLite cannot be stopped otherwise).
SEARCH_SECONDS = 5.0
_PROGRESS_STEPS = 10_000  # SQLite virtual machine steps between two looks at the clock
# One page's LIKE is a single step, costing up to the pattern's length times the page's: look after each page.
_LIKE_PROGRESS_STEPS = 10
# Without the index a query is one substring to find; only its start is matched, bounding each page's LIKE.
LIKE_CHARS = 32


class SearchTooSlow(ServiceUnavailable):
    """A search stopped after SEARCH_SECONDS; answered as 503 with its own message."""

    def __init__(self) -> None:
        super().__init__(t("pages.search.too_slow"))


@contextmanager
def time_limit(steps: int | None = None) -> Iterator[None]:
    """Interrupt the statements run inside on the current connection after SEARCH_SECONDS (SearchTooSlow).

    The clock is read every *steps* virtual machine steps (default _PROGRESS_STEPS); a statement is never
    stopped within one step.
    """
    conn = db.session.conn
    deadline = time.monotonic() + SEARCH_SECONDS
    conn.set_progress_handler(lambda: time.monotonic() > deadline, steps or _PROGRESS_STEPS)
    try:
        yield
    except sqlite3.OperationalError as error:
        # "interrupted", or another message when it stopped SQLite opening the index ("vtable constructor failed")
        if time.monotonic() > deadline:
            raise SearchTooSlow() from error
        raise
    finally:
        conn.set_progress_handler(None, 0)


# Excerpts are made from the start of each page shown, not by FTS5's snippet(), whose cost grows with the square
# of the query's matches in the page (thousands of copies of one word took tens of seconds for one page).
EXCERPT_SCAN = 20_000
# A word of an excerpt; a longer run of characters without a space is cut into pieces that count as words.
_WORD = re.compile(r"\S{1,30}")


def excerpts(phrases: list[str], ids: list[int], tokens: int) -> dict[int, str]:
    """Excerpts of about *tokens* words of the pages *ids*, around the first match of *phrases* (FTS5 phrases).

    Only the first EXCERPT_SCAN characters of a page are searched. Matches are marked by FTS5's highlight()
    on that slice in a private in-memory index, so words match exactly as in search, and the work grows with
    the slice, not with the page or the number of matches. A page without a match there gets its first words.
    """
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    texts = {row["id"]: (row["text"] or "").replace("\x02", "").replace("\x03", "") for row in db.all(
        f"SELECT id, substr(content, 1, {EXCERPT_SCAN}) AS text FROM pages WHERE id IN ({marks})", ids)}
    marked: dict[int, str] = {}
    if phrases and texts:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.execute(f"CREATE VIRTUAL TABLE excerpt USING fts5(text, tokenize='{FTS_TOKENIZE}')")
            conn.executemany("INSERT INTO excerpt (rowid, text) VALUES (?, ?)", texts.items())
            marked = dict(conn.execute(
                "SELECT rowid, highlight(excerpt, 0, char(2), char(3)) FROM excerpt WHERE excerpt MATCH ?",
                (" OR ".join(dict.fromkeys(phrases)),)))
        finally:
            conn.close()
    return {page_id: _excerpt(marked.get(page_id, text), tokens, more=len(text) >= EXCERPT_SCAN)
            for page_id, text in texts.items()}


def _excerpt(text: str, tokens: int, *, more: bool) -> str:
    """About *tokens* words of *text* from just before its first \\x02 mark, with "…" where it is cut.

    Spaces between the words become one space, or one line break where there was one (Markdown lists and
    headings are recognised by the line start).
    """
    words = list(_WORD.finditer(text))
    if not words:
        return ""
    first = next((index for index, word in enumerate(words) if "\x02" in word.group()), 0)
    start = max(0, min(first - 2, len(words) - tokens))
    end = min(len(words), start + tokens)
    parts = [words[start].group()]
    for before, word in itertools.pairwise(words[start:end]):
        gap = text[before.end():word.start()]
        parts.append(("\n" if "\n" in gap else " ") if gap else "")
        parts.append(word.group())
    return ("… " if start else "") + "".join(parts) + (" …" if end < len(words) or more else "")


def search(query: str, *, user: dict[str, Any] | None = None, limit: int = 20, offset: int = 0,
           titles_only: bool = False) -> list[dict[str, Any]]:
    """Full-text search over the pages *user* may see, best matches first."""
    query = (query or "").strip()[:200]
    if not query:
        return []
    where, params = visible_filter(user)
    # A repeated word adds nothing to the result but would cost one more pass over the index.
    tokens = list(dict.fromkeys(fts_words(query)))[:12] if fts_available() else []
    if tokens:
        columns = "{title}" if titles_only else "{title content}"
        phrases = [fts_phrase([token]) for token in tokens]
        match = " AND ".join(f"{columns} : {phrase}" for phrase in phrases)
        with time_limit():
            rows = db.all(
                f"SELECT p.id, p.title, p.slug, p.category_id, p.last_edited_at, bm25(pages_fts, 8.0, 1.0) AS score "
                f"FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
                f"WHERE pages_fts MATCH ? AND {where} ORDER BY score LIMIT ? OFFSET ?",
                [match, *params, limit, offset],
            )
        snippets = excerpts([] if titles_only else phrases, [row["id"] for row in rows], 14)
        return [{**{key: value for key, value in row.items() if key != "score"},
                 "snippet": snippets.get(row["id"], ""), "score": row["score"]} for row in rows]
    if fts_available():
        return []  # only signs and punctuation, which the index does not hold (as on /search)
    sought = query[:LIKE_CHARS].strip()
    like = "%" + sought.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    field = "p.title LIKE ? ESCAPE '\\'" if titles_only else "(p.title LIKE ? ESCAPE '\\' OR p.content LIKE ? ESCAPE '\\')"
    like_params = [like] if titles_only else [like, like]
    with time_limit(_LIKE_PROGRESS_STEPS):
        return db.all(
            f"SELECT p.id, p.title, p.slug, p.category_id, p.last_edited_at, substr(p.content, 1, 240) AS snippet, "
            f"0 AS score FROM pages p WHERE {field} AND {where} "
            f"ORDER BY CASE WHEN p.title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, p.title COLLATE NOCASE LIMIT ? OFFSET ?",
            [*like_params, *params, like, limit, offset],
        )
