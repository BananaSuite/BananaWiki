"""The built-in user guide: a "BananaWiki" category of documentation pages.

The pages live as Markdown files in ``builtin_docs/<language>/<variant>/``
(``NN-<slug>.md``, first line ``# Title``). Spawning replaces the category
tracked in ``site_settings.docs_category_id`` and writes every page through
the pages service, attributed to the system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ... import settings
from ..pages import categories, service

DOCS_ROOT = Path(__file__).parent / "builtin_docs"
CATEGORY_NAME = "BananaWiki"
VARIANTS = ("full", "simplified")
LANGUAGES = ("en", "it")


def pages(variant: str = "full", language: str = "en") -> list[tuple[str, str, str]]:
    """(title, slug, content) of every page of one documentation set, in order."""
    variant = variant if variant in VARIANTS else "full"
    language = language if language in LANGUAGES else "en"
    result = []
    for path in sorted((DOCS_ROOT / language / variant).glob("*.md")):
        content = path.read_text(encoding="utf-8")
        first_line = content.split("\n", 1)[0]
        title = first_line[2:].strip() if first_line.startswith("# ") else path.stem
        slug = path.stem.split("-", 1)[1] if "-" in path.stem else path.stem
        result.append((title, slug, content))
    return result


def _remove_existing(actor_id: str | None) -> None:
    tracked = categories.get(settings.get("docs_category_id"))
    if tracked is not None:
        categories.delete(tracked, page_action="delete", actor_id=actor_id)
    settings.update({"docs_category_id": None})


def spawn(*, variant: str = "full", language: str = "en", actor_id: str | None = None) -> dict[str, Any]:
    """(Re)create the documentation category and return it."""
    documents = pages(variant, language)
    _remove_existing(actor_id)
    category = categories.create(CATEGORY_NAME, actor_id=actor_id)
    settings.update({"docs_category_id": category["id"]})
    for position, (title, slug, content) in enumerate(documents):
        service.create(title, content, category_id=category["id"], author_id=None, slug=slug,
                       sort_order=position, edit_message="Built-in documentation")
    return category
