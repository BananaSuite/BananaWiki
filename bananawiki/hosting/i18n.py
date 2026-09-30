"""Interface language of the portal (English and Italian) and ``t()``.

Order: the visitor's explicit choice (``session["interface_language"]``, the
key 1.4 used, so the choice survives the upgrade), then ``Accept-Language``,
then English.
"""

from __future__ import annotations

from typing import Any

from flask import current_app, g, has_request_context, request, session

from ..core.i18n import FALLBACK, Catalog

LANGUAGES = {"en": "English", "it": "Italiano"}
EXTENSION = "bananawiki.hosting.i18n"


def catalog() -> Catalog:
    return current_app.extensions[EXTENSION]


def current_language() -> str:
    if not has_request_context():
        return FALLBACK
    lang = g.get("lang")
    if lang is None:
        chosen = session.get("interface_language")
        if chosen not in LANGUAGES:
            chosen = request.accept_languages.best_match(list(LANGUAGES)) or FALLBACK
        lang = g.lang = chosen
    return lang


def t(key: str, default: str | None = None, /, **values: Any) -> str:
    return catalog().translate(current_language(), key, default, **values)


def t_lang(lang: str, key: str, default: str | None = None, /, **values: Any) -> str:
    return catalog().translate(lang if lang in LANGUAGES else FALLBACK, key, default, **values)


def js_strings() -> dict[str, str]:
    strings = catalog().strings(FALLBACK)
    localized = catalog().strings(current_language())
    return {key[3:]: localized.get(key, value) for key, value in strings.items() if key.startswith("js.")}
