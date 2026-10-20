"""Interface language of the portal (German, English and Italian) and ``t()``.

Order: the visitor's explicit choice (``session["interface_language"]``, the
key 1.4 used, so the choice survives the upgrade), then ``Accept-Language``
(a bare ``*`` gets English), then English.

Every string may name the platform with ``{service}``: ``t()`` fills it with
``HOSTING_SERVICE_NAME`` unless the caller passes its own value.
"""

from __future__ import annotations

from typing import Any

from flask import current_app, g, has_request_context, request, session

from ..core.i18n import FALLBACK, Catalog

LANGUAGES = {"de": "Deutsch", "en": "English", "it": "Italiano"}
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
            offered = sorted(LANGUAGES, key=lambda code: code != FALLBACK)  # ties ("*") go to the first
            chosen = request.accept_languages.best_match(offered) or FALLBACK
        lang = g.lang = chosen
    return lang


def service_name() -> str:
    """The name the platform goes by (``HOSTING_SERVICE_NAME``)."""
    return current_app.config["HOSTING"].service_name


def t(key: str, default: str | None = None, /, **values: Any) -> str:
    return catalog().translate(current_language(), key, default, **{"service": service_name(), **values})


def tn(key: str, count: Any, /, **values: Any) -> str:
    """Count-dependent text: ``<key>_one`` when *count* is 1 and the catalogue has it, else *key*.

    The plain key holds the general (plural) form.
    """
    values.setdefault("count", count)
    try:
        single = int(count) == 1
    except (TypeError, ValueError):
        single = False
    if single and f"{key}_one" in catalog().strings(current_language()):
        return t(f"{key}_one", **values)
    return t(key, **values)


def t_lang(lang: str, key: str, default: str | None = None, /, **values: Any) -> str:
    return catalog().translate(lang if lang in LANGUAGES else FALLBACK, key, default,
                               **{"service": service_name(), **values})


def js_strings() -> dict[str, str]:
    strings = catalog().strings(FALLBACK)
    localized = catalog().strings(current_language())
    return {key[3:]: localized.get(key, value) for key, value in strings.items() if key.startswith("js.")}
