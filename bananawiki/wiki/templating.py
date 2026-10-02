"""Template globals and filters shared by every page."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, current_app, g, request, url_for
from markupsafe import Markup, escape

from .. import __version__
from ..core import web
from ..core.assets import asset_url
from ..core.timeutil import parse, utcnow
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
    moment = parse(value)
    if moment is None:
        return ""
    return moment.astimezone(site_timezone()).strftime(fmt)


def format_date(value: Any) -> str:
    return format_datetime(value, "%Y-%m-%d")


def datetime_local_input(value: Any) -> str:
    """Value for an ``<input type="datetime-local">`` in the site time zone."""
    return format_datetime(value, "%Y-%m-%dT%H:%M")


def from_local_input(value: str | None) -> datetime | None:
    """Parse a ``datetime-local`` value entered in the site time zone."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=site_timezone())
    return naive


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
            return i18n.t(f"time.{unit}_ago", count=max(1, seconds // size))
    return i18n.t("time.years_ago", count=max(1, seconds // (86400 * 365)))


def human_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def favicon_url() -> str:
    s = settings.load()
    custom = s.get("favicon_custom") or ""
    if not s.get("favicon_enabled"):
        return url_for("static", filename="favicons/banana_yellow.png")
    if s.get("favicon_type") == "custom" and custom.startswith("custom_"):
        return url_for("favicon_file", filename=custom)
    preset = s.get("favicon_type") if s.get("favicon_type") in FAVICON_PRESETS else "yellow"
    return url_for("static", filename=f"favicons/banana_{preset}.png")


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
            items.append({"id": f"{feature.id}:{item.endpoint}", "label": i18n.t(item.label), "url": url,
                          "icon": item.icon, "order": item.order, "badge": badge,
                          "active": request.path.startswith(url) and url != "/"})
    order = [x.strip() for x in (settings.get("sidebar_apps_order") or "").split(",") if x.strip()]
    rank = {key: i for i, key in enumerate(order)}
    items.sort(key=lambda item: (rank.get(item["id"].split(":", 1)[0], 1000 + item["order"]), item["label"]))
    return items


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


def install(app: Flask) -> None:
    app.jinja_env.filters.update(
        markdown=lambda text: Markup(render_markdown(text)),
        datetime=format_datetime,
        date=format_date,
        datetime_local=datetime_local_input,
        time_ago=time_ago,
        filesize=human_bytes,
        json_script=_json_script,
    )

    @app.context_processor
    def _globals() -> dict[str, Any]:
        user = auth.current_user()
        return {
            "t": i18n.t,
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


def template_root() -> Path:
    return Path(__file__).parent / "templates"
