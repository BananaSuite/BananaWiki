"""Interface languages: which are enabled, the default, and uploaded language files.

Uploaded files are stored through the catalogue (``write_custom``) in the
instance directory, never in the source tree. A file for ``en`` or ``it``
overrides bundled strings; deleting it restores them. Every uploaded string
must use exactly the ``{placeholders}`` of the English text, so a translation
can neither drop a value nor ask for one that is not supplied.
"""

from __future__ import annotations

import json
import re
from typing import Any

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.i18n import FALLBACK, META_KEY, valid_code
from ... import settings
from ...i18n import BUILTIN_LANGUAGES, catalog, language_switches

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_VALUE_LENGTH = 5000
MAX_LANGUAGES = 100
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LanguageError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def _custom_codes() -> set[str]:
    folder = catalog().custom_dir
    if not folder or not folder.is_dir():
        return set()
    return {code for code in (valid_code(p.stem) for p in folder.glob("*.json")) if code}


def overview() -> list[dict[str, Any]]:
    """Every installed language with its state, for the languages page."""
    switches = language_switches()
    custom = _custom_codes()
    english = catalog().strings(FALLBACK)
    rows = []
    for code in catalog().languages():
        entry = switches.get(code)
        meta = catalog().meta(code)
        strings = catalog().strings(code)
        rows.append({
            "code": code,
            "name": meta.get("name") or (entry or {}).get("name") or BUILTIN_LANGUAGES.get(code, code),
            "english_name": meta.get("english_name") or "",
            "author": meta.get("author") or "",
            "builtin": code in BUILTIN_LANGUAGES,
            "custom_file": code in custom,
            "enabled": entry["enabled"] if entry else code in BUILTIN_LANGUAGES,
            "coverage": round(100 * len(set(strings) & set(english)) / len(english)) if english else 100,
        })
    return rows


def _save_switches(switches: dict[str, dict[str, Any]]) -> None:
    settings.update({"interface_languages_json": json.dumps(switches, ensure_ascii=False, sort_keys=True,
                                                            separators=(",", ":"))})


def _enabled_codes(switches: dict[str, dict[str, Any]]) -> set[str]:
    return {code for code in catalog().languages()
            if (switches[code]["enabled"] if code in switches else code in BUILTIN_LANGUAGES)}


def set_enabled(code: str, enabled: bool) -> None:
    code = valid_code(code)
    if code not in catalog().languages():
        raise LanguageError("site_admin.languages.error.unknown")
    switches = language_switches()
    name = catalog().meta(code).get("name") or switches.get(code, {}).get("name") or code
    switches[code] = {"name": name, "enabled": bool(enabled)}
    if not enabled:
        if code == default_language():
            raise LanguageError("site_admin.languages.error.default_disabled")
        if not _enabled_codes(switches):
            raise LanguageError("site_admin.languages.error.last")
    _save_switches(switches)


def default_language() -> str:
    return valid_code(settings.get("interface_language")) or current_app.config["BW"].default_language


def set_default(code: str, fallback: str) -> None:
    code = valid_code(code)
    if code not in _enabled_codes(language_switches()):
        raise LanguageError("site_admin.languages.error.not_enabled")
    if fallback not in BUILTIN_LANGUAGES:
        raise LanguageError("site_admin.languages.error.unknown")
    settings.update({"interface_language": code, "interface_language_fallback": fallback})


# ── Language files ───────────────────────────────────────────────────────────


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def validate_pack(payload: Any) -> tuple[str, dict[str, Any], int]:
    """Check a language file; return ``(code, data to store, ignored unknown keys)``."""
    if not isinstance(payload, dict):
        raise LanguageError("site_admin.languages.error.not_object")
    meta = payload.get(META_KEY)
    if not isinstance(meta, dict):
        raise LanguageError("site_admin.languages.error.meta")
    code = valid_code(meta.get("code"))
    name = str(meta.get("name") or "").strip()
    if not code or not name:
        raise LanguageError("site_admin.languages.error.meta")
    english = catalog().strings(FALLBACK)
    data: dict[str, Any] = {META_KEY: {
        "code": code,
        "name": name[:120],
        "english_name": str(meta.get("english_name") or "").strip()[:120] or name[:120],
        "rtl": bool(meta.get("rtl", False)),
        "author": str(meta.get("author") or "").strip()[:120],
    }}
    ignored = 0
    mismatched = []
    for key, value in payload.items():
        if key == META_KEY:
            continue
        if not isinstance(key, str) or not isinstance(value, str):
            raise LanguageError("site_admin.languages.error.not_string", key=str(key)[:80])
        if len(value) > MAX_VALUE_LENGTH:
            raise LanguageError("site_admin.languages.error.value_too_long", key=key[:80], maximum=MAX_VALUE_LENGTH)
        if key not in english:
            ignored += 1
            continue
        if placeholders(value) != placeholders(english[key]):
            mismatched.append(key)
            continue
        data[key] = value
    if mismatched:
        raise LanguageError("site_admin.languages.error.placeholders", keys=", ".join(mismatched[:5]),
                            count=len(mismatched))
    if len(data) == 1:
        raise LanguageError("site_admin.languages.error.empty")
    return code, data, ignored


def read_upload(upload: FileStorage | None) -> Any:
    if upload is None or not upload.filename:
        raise LanguageError("site_admin.languages.error.no_file")
    raw = upload.stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise LanguageError("site_admin.languages.error.too_large", limit_mb=MAX_FILE_BYTES // (1024 * 1024))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LanguageError("site_admin.languages.error.not_json") from None


def install(payload: Any) -> tuple[str, int]:
    """Validate and store a language file; a new language starts enabled."""
    code, data, ignored = validate_pack(payload)
    known = set(catalog().languages())
    if code not in known and len(known) >= MAX_LANGUAGES:
        raise LanguageError("site_admin.languages.error.too_many", limit=MAX_LANGUAGES)
    catalog().write_custom(code, data)
    switches = language_switches()
    if code not in BUILTIN_LANGUAGES and code not in switches:
        switches[code] = {"name": data[META_KEY]["name"], "enabled": True}
        _save_switches(switches)
    return code, ignored


def delete_file(code: str) -> None:
    """Remove an uploaded file; a removed custom language is also switched off."""
    code = valid_code(code)
    if code not in _custom_codes():
        raise LanguageError("site_admin.languages.error.unknown")
    if code not in BUILTIN_LANGUAGES and code == default_language():
        raise LanguageError("site_admin.languages.error.default_delete")
    catalog().delete_custom(code)
    switches = language_switches()
    if code in switches and code not in BUILTIN_LANGUAGES:
        del switches[code]
        _save_switches(switches)


def download(code: str) -> dict[str, Any] | None:
    """A language as a downloadable file: its metadata and every string it has."""
    code = valid_code(code)
    if code not in catalog().languages():
        return None
    meta = catalog().meta(code)
    data: dict[str, Any] = {META_KEY: {
        "code": code,
        "name": meta.get("name") or BUILTIN_LANGUAGES.get(code, code),
        "english_name": meta.get("english_name") or "",
        "rtl": bool(meta.get("rtl", False)),
        "author": meta.get("author") or "",
    }}
    data.update(catalog().strings(code))
    return data
