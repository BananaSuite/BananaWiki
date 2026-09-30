"""Starter layouts offered by the builder for an empty page, in the reader's language."""

from __future__ import annotations

from functools import partial
from typing import Any

from ...i18n import t
from . import document

STARTERS = ("landing", "guide", "faq")


def _s(starter: str, key: str) -> str:
    return t(f"page_builder.starter.{starter}.{key}")


def _landing() -> list[dict[str, Any]]:
    s = partial(_s, "landing")
    return [
        {"type": "hero", "title": s("hero_title"), "text": s("hero_text"), "button_label": s("hero_button"),
         "button_url": "/", "layout": {"align": "center"}},
        {"type": "cards", "columns": 3, "items": [
            {"title": s(f"card{n}_title"), "text": s(f"card{n}_text")} for n in (1, 2, 3)]},
        {"type": "heading", "level": 2, "text": s("faq_heading")},
        {"type": "faq", "items": [{"question": s(f"q{n}"), "answer": s(f"a{n}")} for n in (1, 2)]},
        {"type": "button", "label": s("contact"), "url": "/", "style": "outline", "layout": {"align": "center"}},
    ]


def _guide() -> list[dict[str, Any]]:
    s = partial(_s, "guide")
    return [
        {"type": "heading", "level": 2, "text": s("overview")},
        {"type": "text", "format": "markdown", "text": s("intro")},
        {"type": "callout", "tone": "info", "title": s("before_title"), "text": s("before_text")},
        {"type": "heading", "level": 2, "text": s("steps")},
        {"type": "list", "ordered": True, "items": [s("step1"), s("step2"), s("step3")]},
        {"type": "code", "language": "bash", "code": 'echo "Hello"'},
        {"type": "heading", "level": 2, "text": s("related")},
        {"type": "pages", "source": "recent", "limit": 5, "display": "list", "excerpt": False},
    ]


def _faq() -> list[dict[str, Any]]:
    s = partial(_s, "faq")
    return [
        {"type": "text", "format": "markdown", "text": s("intro")},
        {"type": "faq", "items": [{"question": s(f"q{n}"), "answer": s(f"a{n}")} for n in (1, 2, 3)]},
        {"type": "callout", "tone": "success", "title": s("more_title"), "text": s("more_text")},
    ]


_BUILDERS = {"landing": _landing, "guide": _guide, "faq": _faq}


def starters() -> list[dict[str, Any]]:
    """``[{id, name, description, document}]``; every document is a valid, complete one."""
    return [{"id": starter, "name": _s(starter, "name"), "description": _s(starter, "description"),
             "document": document.validate({"version": document.VERSION, "blocks": _BUILDERS[starter]()})}
            for starter in STARTERS]
