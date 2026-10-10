"""Interface language for the current request, and the ``t()`` helper.

Resolution order: an explicit ``?lang=`` choice (stored in the session), the
signed-in user's saved preference, the visitor's cookie preference, the site
default, then English. Only languages the administrator enabled are used.
"""

from __future__ import annotations

import json
from typing import Any

from flask import current_app, g, has_request_context, request, session

from ..core.i18n import FALLBACK, Catalog, valid_code

BUILTIN_LANGUAGES = {"de": "Deutsch", "en": "English", "it": "Italiano"}


def catalog() -> Catalog:
    return current_app.extensions["bananawiki.i18n"]


def language_switches() -> dict[str, dict[str, Any]]:
    """``interface_languages_json`` as ``code -> {"name", "enabled"}``.

    1.4 stored ``{code: {"name": ..., "enabled": bool}}`` for uploaded
    languages only (built-ins were always on); an entry for a built-in
    language switches it off. Plain booleans are accepted too.
    """
    from . import settings

    try:
        configured = json.loads(settings.get("interface_languages_json") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(configured, dict):
        return {}
    switches: dict[str, dict[str, Any]] = {}
    for raw_code, value in configured.items():
        code = valid_code(raw_code)
        if not code:
            continue
        if isinstance(value, dict):
            switches[code] = {"name": str(value.get("name") or ""), "enabled": bool(value.get("enabled", True))}
        else:
            switches[code] = {"name": "", "enabled": bool(value)}
    return switches


def enabled_languages() -> dict[str, str]:
    """Languages visitors may choose: code -> native name."""
    switches = language_switches()
    chosen = {}
    for code in catalog().languages():
        entry = switches.get(code)
        if entry["enabled"] if entry else code in BUILTIN_LANGUAGES:
            name = catalog().meta(code).get("name") or (entry or {}).get("name")
            chosen[code] = name or BUILTIN_LANGUAGES.get(code, code)
    return chosen or {FALLBACK: "English"}


def default_language() -> str:
    from . import settings

    configured = valid_code(settings.get("interface_language")) or current_app.config["BW"].default_language
    return configured if configured in enabled_languages() else FALLBACK


def _user_preference() -> str | None:
    user = g.get("user")
    if not user or not user.get("accessibility"):
        return None
    try:
        data = json.loads(user["accessibility"])
    except (TypeError, ValueError):
        return None
    return valid_code(data.get("interface_language")) if isinstance(data, dict) else None


def resolve_language() -> str:
    enabled = enabled_languages()
    requested = valid_code(request.args.get("lang"))
    if requested and requested in enabled:
        session["interface_language"] = requested
        return requested
    for candidate in (_user_preference(), valid_code(session.get("interface_language")),
                      valid_code(request.cookies.get("bw_lang"))):
        if candidate and candidate in enabled:
            return candidate
    default = default_language()
    if session.get("user_id"):
        return default
    # A bare "*" matches every language alike and werkzeug takes the first one offered: the default.
    return request.accept_languages.best_match(sorted(enabled, key=lambda code: code != default)) or default


def current_language() -> str:
    if has_request_context():
        lang = g.get("lang")
        if lang is None:
            lang = resolve_language()
            g.lang = lang
        return lang
    return FALLBACK


def t(key: str, default: str | None = None, /, **values: Any) -> str:
    """Translate *key* into the current language (``{name}`` placeholders)."""
    return catalog().translate(current_language(), key, default, **values)


def t_lang(lang: str, key: str, default: str | None = None, /, **values: Any) -> str:
    return catalog().translate(lang, key, default, **values)


def js_strings() -> dict[str, str]:
    """Strings under the ``js.`` prefix, for client-side scripts."""
    strings = catalog().strings(FALLBACK)
    localized = catalog().strings(current_language())
    return {key[3:]: localized.get(key, value) for key, value in strings.items() if key.startswith("js.")}


def is_rtl() -> bool:
    return bool(catalog().meta(current_language()).get("rtl"))
