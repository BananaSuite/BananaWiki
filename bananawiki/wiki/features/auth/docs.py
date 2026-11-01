"""The built-in user guide: a "BananaWiki" category of documentation pages.

The pages live as Markdown files in ``builtin_docs/<language>/<variant>/``
(``NN-<slug>.md``, first line ``# Title``). Spawning replaces the category
tracked in ``site_settings.docs_category_id`` and writes every page through
the pages service, attributed to the system. The pages it wrote are listed in
``builtin_doc_pages`` (see :func:`is_builtin`).

Replacing the guide deletes only those pages, at once and without the page
interceptors, so it is refused while the category also holds a page the
installer did not write or one the acting administrator could not change
(protected, checked out, pending deletion: :func:`access.edit_blocked`).
Those go through the ordinary paths first, which apply their rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ... import accounts, settings
from ...db import db
from ..pages import access, categories, service

DOCS_ROOT = Path(__file__).parent / "builtin_docs"
CATEGORY_NAME = "BananaWiki"
EDIT_MESSAGE = "Built-in documentation"  # the first history entry of every page written here
VARIANTS = ("full", "simplified")
LANGUAGES = ("de", "en", "it")
MAX_NAMED_PAGES = 10  # pages named when a replacement is refused


def pages(variant: str = "full", language: str = "en") -> list[tuple[str, str, str]]:
    """(title, slug, content) of every page of one documentation set, in order."""
    variant = variant if variant in VARIANTS else "full"
    language = language if language in LANGUAGES else "en"
    result = []
    for path in sorted((DOCS_ROOT / language / variant).glob("*.md")):
        content = path.read_text(encoding="utf-8")
        first_line, _, rest = content.partition("\n")
        if first_line.startswith("# "):
            # The heading becomes the page title, which the page already shows as its <h1>.
            title, content = first_line[2:].strip(), rest.lstrip("\n")
        else:
            title = path.stem
        slug = path.stem.split("-", 1)[1] if "-" in path.stem else path.stem
        result.append((title, slug, content))
    return result


def _replaceable(page: dict[str, Any], actor: dict[str, Any] | None) -> bool:
    return is_builtin(page["id"]) and access.edit_blocked(page, actor) is None


def _check_replaceable(tracked: dict[str, Any], actor: dict[str, Any] | None) -> None:
    """Raise :class:`categories.CategoryError` naming the pages replacing the guide would not delete."""
    pages = db.all("SELECT * FROM pages WHERE category_id = ? AND is_home = 0 ORDER BY id", (tracked["id"],))
    kept = [page for page in pages if not _replaceable(page, actor)]
    if not kept:
        return
    named = [page["title"] for page in kept if actor is not None and service.can_view(page, actor)]
    raise categories.CategoryError("auth.docs.replace_refused", name=tracked["name"], count=len(kept),
                                   pages=", ".join(named[:MAX_NAMED_PAGES]) or "-")


def _remove_existing(tracked: dict[str, Any] | None, actor: dict[str, Any] | None) -> None:
    actor_id = actor["id"] if actor else None
    if tracked is not None:
        left = 0
        for page in db.all("SELECT * FROM pages WHERE category_id = ? AND is_home = 0", (tracked["id"],)):
            # Checked again page by page: one may have been protected since _check_replaceable.
            if _replaceable(page, actor):
                service.delete(page, actor_id=actor_id)
            else:
                left += 1
        if not left:
            categories.delete(tracked, page_action="uncategorize", actor_id=actor_id)
    settings.update({"docs_category_id": None})


def spawn(*, variant: str = "full", language: str = "en", actor_id: str | None = None) -> dict[str, Any]:
    """(Re)create the documentation category and return it.

    Raises :class:`categories.CategoryError` when the wiki holds as many
    categories as it may and none is the current guide's, or when the current
    guide's category holds pages replacing it must not delete (see the module
    docstring), before the current guide is removed.
    """
    documents = pages(variant, language)
    actor = accounts.by_id(actor_id) if actor_id else None
    tracked = categories.get(settings.get("docs_category_id"))
    categories.ensure_room(freed=1 if tracked else 0)
    if tracked is not None:
        _check_replaceable(tracked, actor)
    _remove_existing(tracked, actor)
    category = categories.create(CATEGORY_NAME, actor_id=actor_id)
    settings.update({"docs_category_id": category["id"]})
    for position, (title, slug, content) in enumerate(documents):
        page = service.create(title, content, category_id=category["id"], author_id=None, slug=slug,
                              sort_order=position, edit_message=EDIT_MESSAGE)
        db.execute("INSERT OR IGNORE INTO builtin_doc_pages (page_id) VALUES (?)", (page["id"],))
    return category


def is_builtin(page_id: int) -> bool:
    """Whether the installer wrote this page (not whether it is still in the documentation category)."""
    return db.scalar("SELECT 1 FROM builtin_doc_pages WHERE page_id = ?", (page_id,)) is not None
