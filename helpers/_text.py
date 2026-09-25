"""Text slugification helper."""

import re


def slugify(text):
    """Convert *text* to a URL-safe slug.

    Lowercases the text, strips leading/trailing whitespace, removes
    characters that are not word characters, spaces, or hyphens, then
    replaces spaces and underscores with hyphens.  Falls back to
    ``'page'`` if the result would be empty.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = text.strip("-")
    if not text:
        text = "page"
    return text
