"""
BananaWiki: Lightweight JSON-based translation system.

Translations live as flat key/value JSON files at ``translations/<lang>.json``
with dot-separated keys (e.g. ``"auth.login.submit"``).  The :func:`t`
helper resolves keys against the requested language with a fallback chain:

    requested language → English ("en") → raw key

Format strings use ``{placeholder}``-style substitution provided by
``str.format(**kwargs)``.

Files are loaded lazily and cached by :func:`functools.lru_cache`; call
:func:`reload_translations` to clear the cache (e.g. after editing a JSON
file at runtime).

The current language is read from ``flask.g._current_language`` which is
populated by :func:`app.inject_globals`.  When called outside a request
context (e.g. background workers, scripts), pass ``lang=`` explicitly or
fall back to English.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple


_FALLBACK_LANG = "en"
_META_KEY = "_meta"

# Maximum size for an uploaded translation file (bytes).
MAX_TRANSLATION_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# Validates language codes used in the on-disk filenames.  Mirrors the
# validation in ``helpers._interface_languages._normalize_language_code``
# but is re-implemented here to keep this module dependency-free.
_LANG_CODE_RE = re.compile(r"^[a-z]{2,12}(?:-[a-z0-9]{2,8})?$")

# Repository-root /translations directory.  ``helpers/_translations.py`` is
# two levels below the repo root (``helpers/`` then the file itself), so we
# resolve the directory from this file's location, which works whatever the
# current working directory is.
_TRANSLATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "translations",
)


def _translations_dir() -> str:
    """Return the absolute path to the translations directory."""
    return _TRANSLATIONS_DIR


def _safe_lang_code(value: Any) -> str:
    """Return *value* lowercased and validated as a language code, or ``""``."""
    code = str(value or "").strip().lower()
    if not code or not _LANG_CODE_RE.fullmatch(code):
        return ""
    if os.sep in code or "/" in code or ".." in code:
        return ""
    return code


@lru_cache(maxsize=32)
def _load_translations(lang: str) -> Dict[str, Any]:
    """Load and cache the translation dictionary for *lang*.

    Returns an empty dictionary when the file does not exist or is invalid
    so that callers can transparently fall back to English / the raw key.
    """
    safe = _safe_lang_code(lang)
    if not safe:
        return {}
    path = os.path.join(_translations_dir(), f"{safe}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def reload_translations() -> None:
    """Clear the LRU cache so translation files are re-read from disk."""
    _load_translations.cache_clear()


# Language pack file management


def list_translation_files() -> List[str]:
    """Return language codes for every ``translations/<code>.json`` file."""
    out: List[str] = []
    try:
        entries = os.listdir(_translations_dir())
    except OSError:
        return out
    for name in entries:
        if not name.endswith(".json"):
            continue
        code = _safe_lang_code(name[:-5])
        if code:
            out.append(code)
    out.sort()
    return out


def get_language_meta(lang: str) -> Dict[str, Any]:
    """Return the ``_meta`` block for *lang*.

    The block holds metadata such as ``code``, ``name``, ``english_name``,
    ``rtl`` and ``author``.  Missing fields are filled with sensible
    defaults so the UI can render every language consistently.
    """
    safe = _safe_lang_code(lang)
    data = _load_translations(safe) if safe else {}
    raw_meta = data.get(_META_KEY) if isinstance(data, dict) else None
    meta: Dict[str, Any] = {}
    if isinstance(raw_meta, dict):
        meta.update(raw_meta)
    meta.setdefault("code", safe)
    meta.setdefault("name", meta.get("english_name") or safe.upper() or "")
    meta.setdefault("english_name", meta.get("name") or safe.upper() or "")
    meta.setdefault("rtl", bool(meta.get("rtl", False)))
    meta.setdefault("author", meta.get("author", ""))
    meta.setdefault("based_on_version", meta.get("based_on_version", ""))
    return meta


def read_translation_file(lang: str) -> Optional[Dict[str, Any]]:
    """Return the parsed contents of ``translations/<lang>.json`` or ``None``."""
    safe = _safe_lang_code(lang)
    if not safe:
        return None
    path = os.path.join(_translations_dir(), f"{safe}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def validate_language_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate an uploaded language pack payload.

    Returns a ``(data, error)`` tuple.  ``data`` is the normalized dict on
    success (with ``_meta`` populated and ``_meta.code`` lower-cased) or
    ``None`` on failure with a human-readable ``error`` message.
    """
    if not isinstance(payload, dict):
        return None, "Translation file must be a JSON object."
    raw_meta = payload.get(_META_KEY)
    if not isinstance(raw_meta, dict):
        return None, "Missing or invalid '_meta' block."
    code = _safe_lang_code(raw_meta.get("code"))
    if not code:
        return None, "Invalid '_meta.code' (use a 2-12 char language code)."
    name = str(raw_meta.get("name") or "").strip()
    if not name:
        return None, "'_meta.name' is required."
    # Build a normalized output dict (fresh copy to avoid mutating caller's).
    out: Dict[str, Any] = dict(payload)
    meta_norm = {
        "code": code,
        "name": name[:120],
        "english_name": str(raw_meta.get("english_name") or "").strip()[:120] or name[:120],
        "rtl": bool(raw_meta.get("rtl", False)),
        "author": str(raw_meta.get("author") or "").strip()[:120],
        "based_on_version": str(raw_meta.get("based_on_version") or "").strip()[:60],
    }
    out[_META_KEY] = meta_norm
    # Drop any non-string values to keep the file safe to render.
    for key in list(out.keys()):
        if key == _META_KEY:
            continue
        value = out[key]
        if not isinstance(value, str):
            del out[key]
    return out, None


def write_translation_file(lang: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Persist *data* to ``translations/<lang>.json``.

    The caller is expected to have validated *data* via
    :func:`validate_language_payload`.  Returns ``(ok, error)``.
    """
    safe = _safe_lang_code(lang)
    if not safe:
        return False, "Invalid language code."
    path = os.path.join(_translations_dir(), f"{safe}.json")
    try:
        os.makedirs(_translations_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"Could not write language file: {exc}"
    reload_translations()
    return True, None


def delete_translation_file(lang: str, *, allow_builtin: bool = False) -> Tuple[bool, Optional[str]]:
    """Remove ``translations/<lang>.json`` from disk.

    Built-in languages (``en``, ``it``) are protected unless
    ``allow_builtin=True`` is passed explicitly.
    """
    safe = _safe_lang_code(lang)
    if not safe:
        return False, "Invalid language code."
    if not allow_builtin and safe in ("en", "it"):
        return False, "Built-in language packs cannot be deleted."
    path = os.path.join(_translations_dir(), f"{safe}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        return False, "Language pack file not found."
    except OSError as exc:
        return False, f"Could not delete language file: {exc}"
    reload_translations()
    return True, None


def get_js_translations(lang: Optional[str] = None) -> Dict[str, str]:
    """Return translations exposed to JavaScript for real-time language switching.

    The flat dictionary returned here is serialized into a ``<script>`` tag
    in ``base.html`` so that ``window._t('key')`` calls inside ``main.js``
    resolve against the active language.  All keys are included so that
    ``data-i18n`` elements can be updated without a page reload.
    """
    requested = (lang or _current_lang() or _FALLBACK_LANG).strip()
    out: Dict[str, str] = {}
    for source_lang in (_FALLBACK_LANG, requested):
        data = _load_translations(source_lang)
        for k, v in data.items():
            if not isinstance(v, str):
                continue
            out[k] = v
    return out


def _current_lang() -> str:
    """Resolve the active language from ``flask.g``, defaulting to English."""
    try:
        from flask import g  # local import to keep this helper Flask-optional
        lang = getattr(g, "_current_language", None)
        if lang:
            return str(lang)
    except Exception:
        pass
    return _FALLBACK_LANG


def _lookup(lang: str, key: str) -> Optional[str]:
    """Return the translation for *key* in *lang* or ``None`` if missing."""
    if key == _META_KEY or key.startswith(_META_KEY + "."):
        return None
    data = _load_translations(lang)
    value = data.get(key)
    if isinstance(value, str):
        return value
    return None


def t(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Translate *key* into the requested language.

    Resolution order:

    1. Explicit ``lang`` argument (or the current request language).
    2. English (``"en"``) fallback.
    3. The ``default`` keyword argument, if provided.
    4. The raw key itself.

    The ``default`` keyword is treated specially: it is *not* passed to
    :py:meth:`str.format` and is used purely as a last-resort fallback
    when no translation exists.  This lets templates write
    ``t("some.key", default="Inline English fallback.")`` to guarantee
    a sensible string even before translators have caught up.

    Any other ``**kwargs`` are passed to :py:meth:`str.format` so callers
    can do ``t("flash.welcome", name=user.username)`` against a JSON
    value of ``"Welcome, {name}!"``.

    If formatting fails (missing placeholder, malformed string, etc.) the
    unformatted string is returned so the user still sees something useful.
    """
    if not key:
        return ""
    default = kwargs.pop("default", None)
    requested = (lang or _current_lang() or _FALLBACK_LANG).strip()

    value = _lookup(requested, key)
    if value is None and requested != _FALLBACK_LANG:
        value = _lookup(_FALLBACK_LANG, key)
    if value is None:
        if isinstance(default, str):
            value = default
        else:
            value = key

    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return value
    return value
