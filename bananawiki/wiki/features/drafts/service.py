"""Editor drafts: one autosaved draft per (page, user) in the ``drafts`` table.

The editor script saves the title and text while someone types. When they
save the page, their draft is cleared and the other editors whose drafts
were open on that page are credited in the edit summary (their drafts are
cleared too, as in 1.4). ``draft_expiration_hours`` (site setting, 0 = never)
removes drafts nobody touched for that long.

Visual page-builder drafts (``page_builder_drafts``) belong to the page
builder feature and are not handled here.
"""

from __future__ import annotations

from typing import Any

from flask import has_request_context, request

from ....core.timeutil import now_sql
from ... import auth, settings
from ...db import db
from ..pages import access as page_access
from ..pages import service as pages

MAX_TITLE = pages.MAX_TITLE
MAX_CONTENT = pages.MAX_CONTENT


class DraftError(ValueError):
    """A refused draft action; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Reading ──────────────────────────────────────────────────────────────────


def get(page_id: int, user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM drafts WHERE page_id = ? AND user_id = ?", (page_id, user_id))


def others(page_id: int, user_id: str) -> list[dict[str, Any]]:
    """Other people's drafts of *page_id* (who and when, never their text)."""
    return db.all(
        "SELECT d.user_id, d.updated_at, u.username FROM drafts d JOIN users u ON u.id = d.user_id "
        "WHERE d.page_id = ? AND d.user_id != ? ORDER BY d.updated_at DESC",
        (page_id, user_id),
    )


def of_user(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT d.page_id, d.title, d.updated_at, length(d.content) AS size FROM drafts d "
        "WHERE d.user_id = ? ORDER BY d.updated_at DESC",
        (user_id,),
    )


def count_of_user(user_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM drafts WHERE user_id = ?", (user_id,), default=0))


# ── Permissions ──────────────────────────────────────────────────────────────


def _user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    return auth.current_user() if user is None else user


def can_edit(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """The page can be edited by *user* right now (drafts only make sense then)."""
    user = _user(user)
    return bool(user) and pages.can_edit(page, user) and not page_access.edit_blocked(page, user)


def can_save(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return can_edit(page, user) and auth.has_permission("draft.create", user)


def can_transfer(page: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    user = _user(user)
    return can_edit(page, user) and auth.has_permission("draft.transfer", user)


# ── Writing ──────────────────────────────────────────────────────────────────


def _text(value: Any, limit: int, key: str) -> str:
    if not isinstance(value, str):
        raise DraftError("drafts.error.invalid")
    value = value.replace("\r\n", "\n")
    if len(value) > limit:
        raise DraftError(key, limit=limit)
    return value


def save(page: dict[str, Any], user: dict[str, Any], title: Any, content: Any,
         base_revision: int | None = None) -> str:
    """Store the user's draft; return ``"saved"``, ``"unchanged"`` or ``"stale"``.

    * A draft identical to the page is not kept (and an existing one goes).
    * ``"stale"``: the user saved the page after the editor was opened and
      has no draft any more, so this is a late autosave from the editor they
      just left; it must not bring the cleared draft back.
    """
    title = _text(title, MAX_TITLE, "drafts.error.title_too_long")
    content = _text(content, MAX_CONTENT, "drafts.error.content_too_long")
    with db.transaction():
        current = pages.get(page["id"])
        if current is None:
            raise DraftError("drafts.error.page_missing")
        existing = get(page["id"], user["id"])
        if (existing is None and base_revision is not None
                and int(current.get("revision") or 0) > base_revision
                and current.get("last_edited_by") == user["id"]):
            return "stale"
        if title.strip() == (current["title"] or "").strip() and content.strip() == (current["content"] or "").strip():
            if existing is not None:
                delete(page["id"], user["id"])
            return "unchanged"
        now = now_sql()
        db.execute(
            "INSERT INTO drafts (page_id, user_id, title, content, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(page_id, user_id) DO UPDATE SET title = excluded.title, content = excluded.content, "
            "updated_at = excluded.updated_at",
            (page["id"], user["id"], title, content, now),
        )
    return "saved"


def delete(page_id: int, user_id: str) -> bool:
    return db.execute("DELETE FROM drafts WHERE page_id = ? AND user_id = ?", (page_id, user_id)).rowcount > 0


def transfer(page_id: int, from_user_id: str, to_user_id: str) -> None:
    """Give *from_user_id*'s draft to *to_user_id*, replacing the latter's own draft."""
    if from_user_id == to_user_id:
        raise DraftError("drafts.error.transfer_self")
    with db.transaction():
        if get(page_id, from_user_id) is None:
            raise DraftError("drafts.error.missing")
        db.execute("DELETE FROM drafts WHERE page_id = ? AND user_id = ?", (page_id, to_user_id))
        db.execute("UPDATE drafts SET user_id = ?, updated_at = ? WHERE page_id = ? AND user_id = ?",
                   (to_user_id, now_sql(), page_id, from_user_id))


# ── Page saved: clear drafts and credit contributors ─────────────────────────


def _credit(page: dict[str, Any], user: dict[str, Any], names: list[str]) -> None:
    """Append the contributors to the summary of the edit *user* just saved."""
    entry = db.one(
        "SELECT id, edit_message FROM page_history WHERE page_id = ? ORDER BY id DESC LIMIT 1", (page["id"],)
    )
    if entry is None or page.get("last_edited_by") != user["id"]:
        return
    listed = ", ".join(names)
    message = entry["edit_message"] or ""
    message = f"{message} (contributors: {listed})" if message else f"Contributors: {listed}"
    db.execute("UPDATE page_history SET edit_message = ? WHERE id = ?",
               (message[:pages.MAX_EDIT_MESSAGE], entry["id"]))


def on_page_saved(page: dict[str, Any], user: dict[str, Any], **_: Any) -> None:
    """``page.saved`` interceptor: the page was saved (editor, new page, page builder).

    The user's own draft is cleared. When the Markdown editor saved a new
    revision, the other editors' drafts count as merged into it: their
    authors are credited in the edit summary and the drafts are cleared.
    """
    from_editor = has_request_context() and request.endpoint == "pages.edit"
    base = request.form.get("revision", type=int) if from_editor else None
    changed = base is not None and int(page.get("revision") or 0) > base
    with db.transaction():
        delete(page["id"], user["id"])
        if not changed:
            return
        contributors = others(page["id"], user["id"])
        if contributors:
            _credit(page, user, [row["username"] for row in contributors])
            db.execute("DELETE FROM drafts WHERE page_id = ?", (page["id"],))


# ── Expiry ───────────────────────────────────────────────────────────────────


def expire() -> int:
    """Delete drafts untouched for ``draft_expiration_hours`` (background job)."""
    hours = max(0, int(settings.get("draft_expiration_hours", 0) or 0))
    if not hours:
        return 0
    # datetime() also reads the ISO timestamps 1.4 wrote into this column.
    return db.execute(
        "DELETE FROM drafts WHERE datetime(updated_at) < datetime('now', ?)", (f"-{hours} hours",)
    ).rowcount
