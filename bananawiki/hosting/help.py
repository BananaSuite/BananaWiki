"""The built-in help centre (``content/help.<lang>.json``, shipped with the portal).

Articles are trusted HTML written by the project. They carry ``{contact_email}``
and ``{source_url}`` placeholders, filled with this platform's values.
"""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTENT_DIR = Path(__file__).resolve().parent / "content"
LANGUAGES = ("de", "en", "it")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 64 and bool(_SLUG.match(slug))


@lru_cache(maxsize=len(LANGUAGES))
def articles(language: str) -> tuple[dict[str, Any], ...]:
    if language not in LANGUAGES:
        language = "en"
    try:
        raw = json.loads((CONTENT_DIR / f"help.{language}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return () if language == "en" else articles("en")
    return tuple(
        {"slug": item["slug"], "title": item.get("title", item["slug"]), "description": item.get("description", ""),
         "sections": [{"heading": s.get("heading", ""), "html": s.get("html", "")} for s in item.get("sections", [])]}
        for item in raw if isinstance(item, dict) and valid_slug(item.get("slug", ""))
    )


# A link from one article to another (or to the index), with an optional #fragment.
_HELP_LINK = re.compile(r'href="(/help(?:/[a-z0-9-]+)?)(#[^"]*)?"')


def article(language: str, slug: str, *, contact_email: str, source_url: str,
            link_language: str | None = None) -> dict[str, Any] | None:
    """*slug* in *language* (or English), with this platform's values filled in.

    link_language: the language the reader asked for with ``?lang=``; links to other articles keep it.
    """
    if not valid_slug(slug):
        return None
    found = next((a for a in articles(language) if a["slug"] == slug), None)
    if found is None and language != "en":
        found = next((a for a in articles("en") if a["slug"] == slug), None)
    if found is None:
        return None
    values = {"{contact_email}": html.escape(contact_email, quote=True), "{source_url}": html.escape(source_url, quote=True)}

    def fill(text: str) -> str:
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        if link_language in LANGUAGES:
            text = _HELP_LINK.sub(lambda m: f'href="{m.group(1)}?lang={link_language}{m.group(2) or ""}"', text)
        return text

    return {**found, "sections": [{**s, "html": fill(s["html"])} for s in found["sections"]]}
