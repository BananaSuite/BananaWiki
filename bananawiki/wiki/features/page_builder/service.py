"""Who may use the page builder, builder drafts, and publishing.

The builder is another editor for ordinary pages, not a separate permission
system: a user may build a page only when

* the feature is on (``page_builder_enabled``) and the host allows it
  (``BW_FORBID_PAGE_BUILDER`` unset),
* their role reaches the ``page_builder_access`` minimum, and
* the pages service lets them edit that page (``service.can_edit``: editors
  with write access to the category, administrators) and no feature blocks
  editing it (``page.edit_blocked``: protection, reservations, pending
  deletion).

1.4 let plain users publish builder edits to any page they could read; that
is gone. Publishing goes through ``pages.service.update``, so history,
events and edit-conflict detection are the same as for the Markdown editor.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from ....core.timeutil import now_sql
from ... import auth, registry, settings
from ...db import db
from ..pages import service as pages
from . import document

DEFAULT_EDIT_MESSAGE = "Updated with the visual page builder"


def is_active() -> bool:
    return registry.is_enabled("page_builder") and not current_app.config["BW"].forbid_page_builder


def may_use(user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    return bool(user) and is_active() and auth.minimum_role_allows(settings.get("page_builder_access"), user)


def may_build(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    return may_use(user) and pages.can_edit(page, user)


def may_publish_public(user: dict[str, Any]) -> bool:
    """Only administrators may expose a builder page to public-mode visitors, if the host allows it."""
    return auth.is_admin(user) and not current_app.config["BW"].forbid_public_builder_pages


# ── Documents ─────────────────────────────────────────────────────────────────


def page_document(page: dict[str, Any]) -> dict[str, Any]:
    """The document the page shows now (its Markdown as Markdown text blocks if it has none)."""
    raw = page.get("builder_json") or ""
    if raw:
        try:
            loaded = document.load(raw)
        except document.DocumentError:
            loaded = None
        if loaded is not None and document.is_current(page, loaded):
            return loaded
    return document.from_markdown(page.get("content") or "")


def render_page(page: dict[str, Any]) -> Any:
    """``page.render`` interceptor: builder pages render from their document."""
    raw = page.get("builder_json") or ""
    if not raw or not is_active():
        return None
    try:
        loaded = document.load(raw)
    except document.DocumentError:
        return None
    if not document.is_current(page, loaded):
        return None
    return document.render(loaded, page_id=page.get("id"))


# ── Drafts ────────────────────────────────────────────────────────────────────


def get_draft(page_id: int, user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM page_builder_drafts WHERE page_id = ? AND user_id = ?", (page_id, user_id))


def save_draft(page_id: int, user_id: str, encoded: str, base_revision: int) -> bool:
    """Store the user's draft unless the page moved past *base_revision*."""
    with db.transaction():
        revision = db.scalar("SELECT revision FROM pages WHERE id = ?", (page_id,))
        if revision is None or int(revision) != int(base_revision):
            return False
        db.execute(
            "INSERT INTO page_builder_drafts (page_id, user_id, builder_json, updated_at, base_revision) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(page_id, user_id) DO UPDATE SET "
            "builder_json = excluded.builder_json, updated_at = excluded.updated_at, "
            "base_revision = excluded.base_revision",
            (page_id, user_id, encoded, now_sql(), int(base_revision)),
        )
    return True


def delete_draft(page_id: int, user_id: str) -> None:
    db.execute("DELETE FROM page_builder_drafts WHERE page_id = ? AND user_id = ?", (page_id, user_id))


# ── Publishing ────────────────────────────────────────────────────────────────


def publish(page: dict[str, Any], user: dict[str, Any], payload: Any, *, title: str | None,
            edit_message: str, public: bool, base_revision: int) -> dict[str, Any]:
    """Save *payload* as the page's new revision.

    Raises :class:`document.DocumentError`, ``pages.PageError`` or
    ``pages.EditConflict``. A page stays public only when an administrator
    publishes it as public, so edits by others need a fresh approval.
    """
    validated = document.validate(payload, complete=True)
    updated = pages.update(
        page,
        author_id=user["id"],
        title=title if title is not None else page["title"],
        content=document.to_markdown(validated),
        builder_json=document.dump(validated),
        builder_public=bool(public and may_publish_public(user)),
        edit_message=(edit_message or DEFAULT_EDIT_MESSAGE)[: pages.MAX_EDIT_MESSAGE],
        expected_revision=base_revision,
    )
    delete_draft(page["id"], user["id"])
    registry.intercept("page.saved", page=updated, user=user)
    return updated


# ── Saved sections ────────────────────────────────────────────────────────────
#
# Administrators save a run of blocks under a name; everyone who may use the
# builder can then insert it on any page. A section is an ordinary document
# (validated as a draft, since it may hold placeholders to fill in).

MAX_SECTIONS = 100
MAX_SECTION_NAME = 80


class SectionError(ValueError):
    """A refused section; ``key`` is a translation key."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def may_manage_sections(user: dict[str, Any] | None = None) -> bool:
    user = auth.current_user() if user is None else user
    return may_use(user) and auth.is_admin(user)


def sections() -> list[dict[str, Any]]:
    """``[{id, name, document}]`` by name; rows that no longer validate are left out."""
    listed = []
    for row in db.all("SELECT id, name, builder_json FROM page_builder_sections ORDER BY name COLLATE NOCASE, id"):
        try:
            listed.append({"id": row["id"], "name": row["name"], "document": document.load(row["builder_json"])})
        except document.DocumentError:
            continue
    return listed


def create_section(name: Any, payload: Any, user: dict[str, Any]) -> dict[str, Any]:
    """Store *payload* (a document) as a named section. Raises SectionError or DocumentError."""
    if not isinstance(name, str) or not name.strip():
        raise SectionError("page_builder.error.section_name")
    name = " ".join(name.split())
    if len(name) > MAX_SECTION_NAME:
        raise SectionError("page_builder.error.section_name")
    validated = document.validate(payload, complete=False)
    if not validated["blocks"]:
        raise SectionError("page_builder.error.section_empty")
    with db.transaction():
        if int(db.scalar("SELECT COUNT(*) FROM page_builder_sections") or 0) >= MAX_SECTIONS:
            raise SectionError("page_builder.error.section_limit")
        section_id = db.insert("page_builder_sections", {
            "name": name, "builder_json": document.dump(validated), "created_by": user["id"], "created_at": now_sql(),
        })
    return {"id": section_id, "name": name, "document": validated}


def delete_section(section_id: int) -> bool:
    return db.execute("DELETE FROM page_builder_sections WHERE id = ?", (section_id,)).rowcount > 0
