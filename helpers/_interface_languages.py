"""Interface language helpers (built-ins + custom packs)."""

from collections import OrderedDict
import json
import re


BUILTIN_INTERFACE_LANGUAGES = OrderedDict((
    ("en", "English"),
    ("it", "Italiano"),
))

_LANG_CODE_RE = re.compile(r"^[a-z]{2,12}(?:-[a-z0-9]{2,8})?$")
_MAX_CUSTOM_LANGUAGES = 100
_MAX_LANGUAGE_NAME = 60


def _normalize_language_code(value):
    """Return a sanitized language code or empty string."""
    code = str(value or "").strip().lower()
    if not _LANG_CODE_RE.fullmatch(code):
        return ""
    return code


def _normalize_language_name(value):
    """Return a sanitized display name for a language."""
    name = str(value or "").strip()
    if not name:
        return ""
    return name[:_MAX_LANGUAGE_NAME]


def parse_custom_interface_languages(raw_json):
    """Parse custom language JSON into a normalized dict."""
    try:
        payload = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    out = {}
    for code, meta in payload.items():
        if len(out) >= _MAX_CUSTOM_LANGUAGES:
            break
        code_norm = _normalize_language_code(code)
        if not code_norm or code_norm in BUILTIN_INTERFACE_LANGUAGES:
            continue
        if not isinstance(meta, dict):
            continue
        name = _normalize_language_name(meta.get("name"))
        if not name:
            continue
        out[code_norm] = {
            "name": name,
            "enabled": bool(meta.get("enabled", True)),
        }
    return out


def dump_custom_interface_languages(custom_languages):
    """Serialize custom language metadata as compact JSON."""
    if not custom_languages:
        return "{}"
    return json.dumps(custom_languages, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def get_custom_interface_languages(settings):
    """Return normalized custom language dict from site settings."""
    settings = settings or {}
    return parse_custom_interface_languages(settings.get("interface_languages_json", "{}"))


def get_enabled_interface_languages(settings):
    """Return an ordered code->display_name map of enabled languages."""
    languages = OrderedDict(BUILTIN_INTERFACE_LANGUAGES)
    custom = get_custom_interface_languages(settings)
    for code in sorted(custom):
        if custom[code]["enabled"]:
            languages[code] = custom[code]["name"]
    return languages


def get_all_interface_languages(settings):
    """Return list of all languages with metadata for admin UI."""
    rows = []
    for code, name in BUILTIN_INTERFACE_LANGUAGES.items():
        rows.append({
            "code": code,
            "name": name,
            "enabled": True,
            "builtin": True,
        })
    custom = get_custom_interface_languages(settings)
    for code in sorted(custom):
        meta = custom[code]
        rows.append({
            "code": code,
            "name": meta["name"],
            "enabled": bool(meta["enabled"]),
            "builtin": False,
        })
    return rows


def normalize_language_selection(value, settings, default="en", allow_default=False):
    """Normalize a language selection value against enabled languages."""
    raw = str(value or "").strip().lower()
    if allow_default and raw == "default":
        return "default"
    value = _normalize_language_code(raw)
    enabled = get_enabled_interface_languages(settings)
    if value in enabled:
        return value
    return default


def normalize_docs_language(value, settings, default="en"):
    """Normalize docs language; custom languages fallback to en/it."""
    value = _normalize_language_code(value)
    if value in BUILTIN_INTERFACE_LANGUAGES:
        return value

    settings = settings or {}
    fallback = _normalize_language_code(settings.get("interface_language_fallback", "en"))
    if fallback not in BUILTIN_INTERFACE_LANGUAGES:
        fallback = "en"

    enabled = get_enabled_interface_languages(settings)
    if value in enabled:
        return fallback
    if default in BUILTIN_INTERFACE_LANGUAGES:
        return default
    return fallback


def upsert_custom_interface_language(settings, code, name, enabled=True):
    """Insert/update a custom language and return serialized JSON."""
    code = _normalize_language_code(code)
    name = _normalize_language_name(name)
    if not code or code in BUILTIN_INTERFACE_LANGUAGES:
        raise ValueError("Invalid language code.")
    if not name:
        raise ValueError("Language name is required.")
    custom = get_custom_interface_languages(settings)
    custom[code] = {"name": name, "enabled": bool(enabled)}
    return dump_custom_interface_languages(custom)


def set_custom_interface_language_enabled(settings, code, enabled):
    """Toggle enabled state for a custom language and return JSON."""
    code = _normalize_language_code(code)
    custom = get_custom_interface_languages(settings)
    if code not in custom:
        raise ValueError("Language not found.")
    custom[code]["enabled"] = bool(enabled)
    return dump_custom_interface_languages(custom)


def delete_custom_interface_language(settings, code):
    """Delete a custom language and return JSON."""
    code = _normalize_language_code(code)
    custom = get_custom_interface_languages(settings)
    if code not in custom:
        raise ValueError("Language not found.")
    del custom[code]
    return dump_custom_interface_languages(custom)


def get_interface_language_label(code, settings, default_label="Default"):
    """Return display label for a language code."""
    code = str(code or "").strip().lower()
    if code == "default":
        return default_label
    enabled = get_enabled_interface_languages(settings)
    return enabled.get(code, code.upper() if code else default_label)


def match_best_interface_language(accept_language_header, enabled_languages):
    """Return the best matched language code from Accept-Language header."""
    if not accept_language_header or not enabled_languages:
        return None

    # Parse Accept-Language: e.g. "en-GB,en;q=0.9,it;q=0.8"
    try:
        candidates = []
        for part in accept_language_header.split(","):
            subparts = part.split(";")
            lang = subparts[0].strip().lower()
            q = 1.0
            for sub in subparts[1:]:
                if sub.strip().startswith("q="):
                    try:
                        q = float(sub.strip()[2:])
                    except ValueError:
                        pass
            candidates.append((lang, q))
        candidates.sort(key=lambda x: x[1], reverse=True)

        enabled_codes = set(enabled_languages.keys())
        for lang, _q in candidates:
            # Exact match
            if lang in enabled_codes:
                return lang
            # Prefix match (e.g. en-us -> en)
            base = lang.split("-")[0]
            if base in enabled_codes:
                return base
    except Exception:
        pass
    return None
