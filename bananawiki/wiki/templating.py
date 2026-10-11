"""Template globals and filters shared by every page."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, current_app, g, request, url_for
from markupsafe import Markup, escape

from .. import __version__
from ..core import web
from ..core.assets import asset_url
from ..core.timeutil import in_zone, parse, utcnow
from . import attention, auth, i18n, registry, settings
from .markdown import render as render_markdown

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
FAVICON_PRESETS = ("yellow", "green", "red", "blue", "purple", "orange", "cyan", "lime")

THEME_DEFAULTS = {
    "dark": {"primary": "#8fa0d4", "secondary": "#1e1e2c", "accent": "#7e9ada", "text": "#c8ccd8",
             "sidebar": "#1a1a24", "bg": "#16161f"},
    "light": {"primary": "#4b63b6", "secondary": "#ffffff", "accent": "#3553c7", "text": "#202534",
              "sidebar": "#e9edf5", "bg": "#f6f7fb"},
}


def site_timezone() -> ZoneInfo:
    name = settings.get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def format_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """*value* in the site time zone; a date too far out for it is shown in UTC."""
    moment = parse(value)
    if moment is None:
        return ""
    return in_zone(moment, site_timezone()).strftime(fmt)


def format_date(value: Any) -> str:
    return format_datetime(value, "%Y-%m-%d")


def datetime_local_input(value: Any) -> str:
    """Value for an ``<input type="datetime-local">`` in the site time zone."""
    return format_datetime(value, "%Y-%m-%dT%H:%M")


def from_local_input(value: str | None) -> datetime | None:
    """Parse a ``datetime-local`` value entered in the site time zone (None if invalid)."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=site_timezone())
    return parse(naive, bounded=True)  # refuses the years that cannot be stored or shown safely


def time_ago(value: Any) -> str:
    moment = parse(value)
    if moment is None:
        return ""
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 0:
        return i18n.t("time.in_future")
    for limit, unit, size in ((60, "seconds", 1), (3600, "minutes", 60), (86400, "hours", 3600),
                              (86400 * 30, "days", 86400), (86400 * 365, "months", 86400 * 30)):
        if seconds < limit:
            return i18n.tn(f"time.{unit}_ago", max(1, seconds // size))
    return i18n.tn("time.years_ago", max(1, seconds // (86400 * 365)))


# Languages that write a decimal comma ("2,3 KB"); the others use a point.
DECIMAL_COMMA = frozenset({"it", "de", "fr", "es", "pt", "nl", "pl", "cs", "ru", "uk", "tr", "sv", "da", "fi", "nb", "ro",
                           "hu", "sk", "sl", "hr", "el", "bg", "ca", "id", "vi"})


def human_bytes(value: Any) -> str:
    """A file size for people: ``900 B``, ``2.3 KB``, ``5 MB`` (``2,3 KB`` in Italian)."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    unit = "B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            break
        size /= 1024
    if unit == "B":
        return f"{size:.0f} B"
    text = f"{size:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    if i18n.current_language().split("-", 1)[0] in DECIMAL_COMMA:
        text = text.replace(".", ",")
    return f"{text} {unit}"


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Opera", "Opera"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
             ("HeadlessChrome", "Chrome (headless)"), ("CriOS", "Chrome"), ("Chromium", "Chromium"),
             ("Chrome/", "Chrome"), ("Safari/", "Safari"), ("curl/", "curl"), ("python-requests", "Python"),
             ("Wget", "Wget"))
_SYSTEMS = (("Windows", "Windows"), ("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"), ("CrOS", "ChromeOS"),
            ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Linux", "Linux"), ("X11", "Unix"))


def device_label(user_agent: Any) -> str:
    """A short "Browser · System" label for a User-Agent string ("Chrome · Linux"); the raw text is kept
    for a tooltip. Unknown agents are shortened to 60 characters with an ellipsis."""
    text = str(user_agent or "").strip()
    if not text:
        return ""
    browser = next((name for marker, name in _BROWSERS if marker in text), "")
    system = next((name for marker, name in _SYSTEMS if marker in text), "")
    if browser or system:
        return " · ".join(part for part in (browser, system) if part)
    return text if len(text) <= 60 else text[:59].rstrip() + "…"


def no_break(value: Any) -> str:
    """*value* (a date or time in running text) kept on one line: non-breaking hyphens and spaces."""
    return str(value or "").replace("-", "\u2011").replace(" ", "\u00a0")


def favicon_url() -> str:
    """The chosen icon; a bundled one carries its content version, so browsers drop an earlier drawing."""
    s = settings.load()
    custom = s.get("favicon_custom") or ""
    if not s.get("favicon_enabled"):
        return asset_url("favicons/banana_yellow.png")
    if s.get("favicon_type") == "custom" and custom.startswith("custom_"):
        return url_for("favicon_file", filename=custom)
    preset = s.get("favicon_type") if s.get("favicon_type") in FAVICON_PRESETS else "yellow"
    return asset_url(f"favicons/banana_{preset}.png")


def _luminance(hex_color: str) -> float:
    """Relative luminance (WCAG) of a #rrggbb or #rgb colour."""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        return 0.0
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def theme_css() -> Markup:
    """CSS custom properties from the administrator's colour choices."""
    s = settings.load()

    def color(key: str, fallback: str) -> str:
        value = str(s.get(key) or "")
        return value if HEX_COLOR.match(value) else fallback

    blocks = []
    for mode, prefix, selector in (("dark", "", ":root, [data-theme='dark']"),
                                   ("light", "light_", "[data-theme='light']")):
        defaults = THEME_DEFAULTS[mode]
        props = ";".join(
            f"--bw-{name}:{color(f'{prefix}{name}_color', fallback)}" for name, fallback in defaults.items()
        )
        # Text on primary buttons follows the chosen colour (0.19 is where white and near-black
        # give the same contrast), so a dark or a light primary both keep readable labels.
        on_primary = "#10131c" if _luminance(color(f"{prefix}primary_color", defaults["primary"])) > 0.19 else "#ffffff"
        blocks.append(f"{selector}{{{props};--on-primary:{on_primary}}}")
    return Markup("".join(blocks))


def nav_items(area: str) -> list[dict[str, Any]]:
    user = auth.current_user()
    items = []
    reg = registry.registry()
    for feature in reg.ordered():
        if not registry.is_enabled(feature.id):
            continue
        for item in feature.nav:
            if item.area != area:
                continue
            try:
                if item.visible is not None and not item.visible(user):
                    continue
                url = url_for(item.endpoint)
            except Exception:  # noqa: BLE001, S112 - a broken nav entry must not break every page
                continue
            badge = 0
            if item.badge is not None and user:
                try:
                    badge = int(item.badge(user) or 0)
                except Exception:  # noqa: BLE001
                    badge = 0
            prefixes = [url]
            for endpoint in item.active_endpoints:
                try:
                    prefixes.append(url_for(endpoint))
                except Exception:  # noqa: BLE001, S112 - an unknown endpoint only loses the highlight
                    continue
            path = request.script_root + request.path
            match = max((len(prefix.rstrip("/")) for prefix in prefixes if _path_within(path, prefix)), default=0)
            items.append({"id": f"{feature.id}:{item.endpoint}", "label": i18n.t(item.label), "url": url,
                          "icon": item.icon, "order": item.order, "badge": badge, "active": bool(match),
                          "_match": match})
    # Only the most specific link is current: /admin/users/merge is "Account merges", not also "Users".
    best = max((item["_match"] for item in items), default=0)
    for item in items:
        item["active"] = bool(best) and item.pop("_match") == best
    order = [x.strip() for x in (settings.get("sidebar_apps_order") or "").split(",") if x.strip()]
    rank = {key: i for i, key in enumerate(order)}
    items.sort(key=lambda item: (rank.get(item["id"].split(":", 1)[0], 1000 + item["order"]), item["label"]))
    return items


def _path_within(path: str, prefix: str) -> bool:
    """True when *path* is *prefix* or below it on a segment boundary.

    ``/admin/bulk-markdown`` is not inside ``/admin/bulk``; ``/`` never matches everything.
    """
    base = prefix.rstrip("/")
    if not base:
        return False
    return path in (base, prefix) or path.startswith(base + "/")


class Prefs(dict):
    __getattr__ = dict.get


def _saved_display_prefs() -> dict[str, Any]:
    """The account's ``accessibility`` JSON, or the visitor's preference cookie."""
    user = auth.current_user()
    raw = user.get("accessibility") if user else request.cookies.get("bw_public_accessibility")
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _pref_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def accessibility_prefs() -> Prefs:
    """Display preferences: saved on the account, or in a cookie for visitors.

    The JSON keeps the 1.4 shape (``theme_mode`` default/dark/light, a numeric
    ``font_scale``, ``contrast`` 0-5, ``line_height`` 0-2, ``reduce_motion``
    and ``dyslexic_font`` flags). The users feature renders the finer values
    (exact text size, colours, background) as CSS.
    """
    saved = _saved_display_prefs()
    theme = saved.get("theme_mode")
    if theme not in ("dark", "light"):
        theme = request.cookies.get("bw_theme")
    if theme not in ("dark", "light"):
        theme = settings.get("default_theme_mode") or "dark"
    scale = _pref_number(saved.get("font_scale"), 1.0)
    return Prefs(
        theme=theme,
        font_scale="xlarge" if scale >= 1.25 else "large" if scale > 1.0 else "normal",
        contrast="high" if _pref_number(saved.get("contrast")) >= 1 else "normal",
        line_height="relaxed" if _pref_number(saved.get("line_height")) >= 1 else "normal",
        reduce_motion=bool(_pref_number(saved.get("reduce_motion"))),
        dyslexic=bool(_pref_number(saved.get("dyslexic_font"))),
    )


def _json_script(value: Any) -> Markup:
    """Serialise *value* for a ``<script type="application/json">`` block."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return Markup(text)


_CODE_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def translate_with_code(key: str, /, **codes: str) -> Markup:
    """A translated sentence whose ``{name}`` placeholders become ``<code>`` identifiers.

    For prose that names API parameters and headers: the names stay untranslated and monospace,
    the words around them follow the language. The sentence itself is escaped.
    """
    text = i18n.t(key)
    out = Markup()
    position = 0
    for match in _CODE_PLACEHOLDER.finditer(text):
        out += escape(text[position:match.start()])
        name = match.group(1)
        out += Markup("<code>{}</code>").format(codes[name]) if name in codes else escape(match.group(0))
        position = match.end()
    return out + escape(text[position:])


def capfirst(value: Any) -> Any:
    """Upper-case only the first letter (a sentence fragment shown on its own, e.g. a history summary);
    unlike Jinja's ``capitalize`` the rest is left alone, and Markup stays Markup."""
    if not value:
        return value
    if isinstance(value, Markup):
        text = str(value)
        return Markup(text[:1].upper() + text[1:]) if not text.startswith("<") else value
    text = str(value)
    return text[:1].upper() + text[1:]


def group_digits(value: Any) -> str:
    """An integer with the language's thousands separator: ``2,457`` (``2.457`` in Italian)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    text = f"{number:,}"
    if i18n.current_language().split("-", 1)[0] in DECIMAL_COMMA:
        text = text.replace(",", ".")
    return text


def break_after_slashes(value: Any) -> Markup:
    """A URL path that may wrap only after a ``/`` (``/<wbr>``), never inside a segment; each part is escaped."""
    return Markup("/<wbr>").join(escape(part) for part in str(value or "").split("/"))


def install(app: Flask) -> None:
    app.jinja_env.filters.update(
        capfirst=capfirst,
        path_breaks=break_after_slashes,
        digits=group_digits,
        markdown=lambda text: Markup(render_markdown(text)),
        datetime=format_datetime,
        date=format_date,
        datetime_local=datetime_local_input,
        time_ago=time_ago,
        filesize=human_bytes,
        device=device_label,
        nobreak=no_break,
        json_script=_json_script,
    )

    @app.context_processor
    def _globals() -> dict[str, Any]:
        user = auth.current_user()
        return {
            "t": i18n.t,
            "tn": i18n.tn,
            "t_code": translate_with_code,
            "lang": i18n.current_language(),
            "rtl": i18n.is_rtl(),
            "languages": i18n.enabled_languages,
            "csrf_token": web.csrf_token,
            "csp_nonce": web.csp_nonce,
            "current_user": user,
            "real_user": auth.real_user(),
            "impersonating": auth.is_impersonating(),
            "has_permission": auth.has_permission,
            "has_role": auth.has_role,
            "is_admin": auth.is_admin(user) if user else False,
            "site": settings.load,
            "site_name": settings.site_name,
            "public_mode": settings.public_mode_active,
            "feature_enabled": registry.is_enabled,
            "nav_items": nav_items,
            "attention_items": attention.items,
            "slot": registry.render_slot,
            "asset_url": asset_url,
            "favicon_url": favicon_url,
            "theme_css": theme_css,
            "js_strings": i18n.js_strings,
            "app_version": __version__,
            "source_url": current_app.config["BW"].source_url,
            "request_id": g.get("request_id", ""),
            "escape": escape,
            "accessibility_prefs": accessibility_prefs,
        }


