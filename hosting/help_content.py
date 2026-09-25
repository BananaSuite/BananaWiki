"""Built-in help centre for the hosting portal.

The articles used to live in the private website repository, which meant the
only people who got a help centre were the ones running bananawiki.com.
Anybody else deploying the platform had a Help link that pointed at a GitHub
source tree. The articles ship with the platform now, in both interface
languages, and the portal renders them itself.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

CONTENT_DIR = Path(__file__).resolve().parent / "content"
LANGUAGES = ("en", "it")
FALLBACK_LANGUAGE = "en"

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def is_valid_slug(slug):
    return bool(slug) and len(slug) <= 64 and _SLUG.fullmatch(slug) is not None


@lru_cache(maxsize=len(LANGUAGES))
def load_articles(language):
    """Return the articles for *language*, falling back to English.

    Parsed once per language and cached: the files ship with the application
    and cannot change while it runs.
    """
    if language not in LANGUAGES:
        language = FALLBACK_LANGUAGE
    path = CONTENT_DIR / f"help.{language}.json"
    try:
        articles = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if language == FALLBACK_LANGUAGE:
            return ()
        return load_articles(FALLBACK_LANGUAGE)
    valid = []
    for article in articles:
        slug = article.get("slug", "")
        if not is_valid_slug(slug):
            continue
        valid.append({
            "slug": slug,
            "title": article.get("title", slug),
            "description": article.get("description", ""),
            "sections": [
                {"heading": section.get("heading", ""), "html": section.get("html", "")}
                for section in article.get("sections", [])
            ],
        })
    return tuple(valid)


def get_article(language, slug):
    """Return one article, or None. Falls back to the English copy so a
    translation gap shows the article rather than a 404."""
    if not is_valid_slug(slug):
        return None
    for article in load_articles(language):
        if article["slug"] == slug:
            return article
    if language != FALLBACK_LANGUAGE:
        for article in load_articles(FALLBACK_LANGUAGE):
            if article["slug"] == slug:
                return article
    return None


def fill_placeholders(article, contact_email, source_url=""):
    """Return a copy of *article* with the operator's details filled in.

    The articles ship with the platform and cannot know who runs a given
    deployment, so they carry ``{contact_email}`` and ``{source_url}`` (the
    source this deployment serves, as AGPL section 13 asks) rather than one
    operator's values.
    """
    from html import escape

    values = {
        "{contact_email}": escape(contact_email or "", quote=True),
        "{source_url}": escape(source_url or "", quote=True),
    }

    def fill(html):
        for placeholder, value in values.items():
            html = html.replace(placeholder, value)
        return html

    return {
        **article,
        "sections": [{**section, "html": fill(section["html"])} for section in article["sections"]],
    }
