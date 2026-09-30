"""Display and accessibility preferences.

Stored as JSON in ``users.accessibility`` for accounts and in the
``bw_public_accessibility`` cookie for anonymous visitors (public mode), in the
exact shape BananaWiki 1.4 used, so saved choices survive the upgrade:

* ``theme_mode`` ``default|dark|light`` and ``interface_language``;
* ``font_scale`` (0.85 … 1.35), ``contrast`` (0-5), ``line_height`` and
  ``letter_spacing`` (0-2), ``reduce_motion`` (0/1), ``dyslexic_font`` (0/1, new);
* ``custom_*`` colours, ``background_image`` (``backgrounds/<hex>.jpg``);
* ``semantic_*`` highlighting levels (0-2), ``sidebar_width`` and
  ``content_max_width`` in pixels.

The page shell reads the coarse switches (``templating.accessibility_prefs``);
:func:`custom_css` renders everything finer as CSS custom properties.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import tempfile
from typing import Any

from flask import Response, current_app, request

from ... import storage
from ...db import db
from ...i18n import enabled_languages
from . import service

COOKIE = "bw_public_accessibility"
COOKIE_MAX_AGE = 365 * 86400
BACKGROUND_DIR = "backgrounds"
BACKGROUND_RE = re.compile(r"^backgrounds/[0-9a-f]{32}\.jpe?g$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RGB_RE = re.compile(r"^rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)$")

FONT_SCALES = (0.85, 0.9, 1.0, 1.1, 1.2, 1.35)
COLOR_KEYS = ("custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar")
SEMANTIC_KEYS = ("semantic_bold", "semantic_italic", "semantic_code", "semantic_link", "semantic_heading")
# Settings the 1.4 editor stored and 1.6 keeps untouched.
PASSTHROUGH_KEYS = ("editor_pane_width", "editor_height", "sidebar_apps_order")

DEFAULTS: dict[str, Any] = {
    "theme_mode": "default",
    "interface_language": "default",
    "font_scale": 1.0,
    "contrast": 0,
    "line_height": 0,
    "letter_spacing": 0,
    "reduce_motion": 0,
    "dyslexic_font": 0,
    "sidebar_width": 0,
    "content_max_width": 0,
    "background_image": "",
    **{key: "" for key in COLOR_KEYS},
    **{key: 0 for key in SEMANTIC_KEYS},
}
_CSS_VARS = {"custom_bg": "--bw-bg", "custom_text": "--bw-text", "custom_primary": "--bw-primary",
             "custom_secondary": "--bw-secondary", "custom_accent": "--bw-accent", "custom_sidebar": "--bw-sidebar"}


def parse(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stored(user: dict[str, Any] | None) -> dict[str, Any]:
    """Raw saved preferences of *user*, or of the anonymous visitor."""
    if user is not None:
        return parse(user.get("accessibility"))
    return parse(request.cookies.get(COOKIE))


def _int(value: Any, allowed: range | tuple[int, ...], default: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number in allowed else default


def _color(value: Any) -> str:
    text = str(value or "").strip()
    return text if COLOR_RE.fullmatch(text) or RGB_RE.fullmatch(text) else ""


def _pixels(value: Any, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(low, min(high, number)) if number > 0 else 0


def clean(data: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated preferences: *data* over *current* over the defaults."""
    merged = {**DEFAULTS, **(current or {}), **data}
    theme = str(merged["theme_mode"]).strip().lower()
    language = str(merged["interface_language"]).strip().lower()
    try:
        scale = float(merged["font_scale"])
    except (TypeError, ValueError):
        scale = 1.0
    background = str(merged["background_image"] or "")
    result: dict[str, Any] = {
        "theme_mode": theme if theme in ("default", "dark", "light") else "default",
        "interface_language": language if language in enabled_languages() else "default",
        "font_scale": min(FONT_SCALES, key=lambda option: abs(option - scale)),
        "contrast": _int(merged["contrast"], range(6)),
        "line_height": _int(merged["line_height"], range(3)),
        "letter_spacing": _int(merged["letter_spacing"], range(3)),
        "reduce_motion": _int(merged["reduce_motion"], (0, 1)),
        "dyslexic_font": _int(merged["dyslexic_font"], (0, 1)),
        "sidebar_width": _pixels(merged["sidebar_width"], 180, 500),
        "content_max_width": _pixels(merged["content_max_width"], 600, 5000),
        "background_image": background if BACKGROUND_RE.fullmatch(background) else "",
    }
    result.update({key: _color(merged[key]) for key in COLOR_KEYS})
    result.update({key: _int(merged[key], range(3)) for key in SEMANTIC_KEYS})
    result.update({key: merged[key] for key in PASSTHROUGH_KEYS if key in merged})
    return result


def current(user: dict[str, Any] | None) -> dict[str, Any]:
    return clean({}, stored(user))


def save(user: dict[str, Any] | None, prefs: dict[str, Any], response: Response) -> None:
    """Persist on the account, or in the visitor's cookie."""
    if user is not None:
        db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps(prefs), user["id"]))
        return
    response.set_cookie(COOKIE, json.dumps(prefs, separators=(",", ":")), max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="Lax", secure=request.is_secure)


def reset(user: dict[str, Any] | None, response: Response) -> None:
    previous = current(user)
    if user is not None:
        keep = {key: previous[key] for key in ("interface_language",) if key in previous}
        save(user, clean(keep), response)
        service.delete_upload(previous.get("background_image"))
    else:
        response.delete_cookie(COOKIE, samesite="Lax")


# ── Background image ──────────────────────────────────────────────────────────


def save_background(user: dict[str, Any], upload: Any) -> str:
    """Store a background picture as a JPEG no larger than the configured limits."""
    from PIL import Image, ImageOps

    cfg = current_app.config["BW"]
    name, path = service.store_image(upload, BACKGROUND_DIR, max_bytes=cfg.background_image_max_upload)
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height > cfg.background_image_max_pixels:
                raise service.ProfileError("users.error.background_too_large")
            picture = ImageOps.exif_transpose(image).convert("RGBA")
        flat = Image.new("RGBA", picture.size, (255, 255, 255, 255))
        flat.alpha_composite(picture)
        flat = flat.convert("RGB")
        flat.thumbnail((cfg.background_image_max_dimension,) * 2, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        flat.save(buffer, format="JPEG", quality=82, optimize=True, progressive=True)
    finally:
        service.delete_upload(name)
    final = f"{BACKGROUND_DIR}/{secrets.token_hex(16)}.jpg"
    folder = storage.folder_path("uploads") / BACKGROUND_DIR
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".write-")
    with os.fdopen(fd, "wb") as out:
        out.write(buffer.getvalue())
    os.replace(tmp, storage.folder_path("uploads") / final)
    previous = current(user)
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
               (json.dumps(clean({"background_image": final}, previous)), user["id"]))
    service.delete_upload(previous.get("background_image"))
    return final


def remove_background(user: dict[str, Any]) -> None:
    previous = current(user)
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
               (json.dumps(clean({"background_image": ""}, previous)), user["id"]))
    service.delete_upload(previous.get("background_image"))


# ── CSS ───────────────────────────────────────────────────────────────────────

_SEMANTIC_CSS = {
    "semantic_bold": (".prose strong, .prose b", ("font-weight:800", "font-weight:900;color:var(--bw-accent)")),
    "semantic_italic": (".prose em, .prose i", ("font-style:italic;letter-spacing:.02em",
                                                "font-style:italic;background:var(--info-bg)")),
    "semantic_code": (".prose code", ("outline:1px solid var(--border-strong)",
                                      "outline:2px solid var(--bw-accent)")),
    "semantic_link": (".prose a", ("text-decoration:underline",
                                   "text-decoration:underline;text-decoration-thickness:2px;font-weight:700")),
    "semantic_heading": (".prose h1, .prose h2, .prose h3, .prose h4", (
        "border-left:3px solid var(--bw-primary);padding-left:.4rem",
        "border-left:6px solid var(--bw-accent);padding-left:.6rem;color:var(--bw-accent)")),
}


def custom_css(prefs: dict[str, Any], background_url: str = "") -> str:
    """CSS for the preferences the page shell does not cover (already validated values)."""
    props = [f"--font-scale:{prefs['font_scale']}"]
    props += [f"{_CSS_VARS[key]}:{prefs[key]}" for key in COLOR_KEYS if prefs.get(key)]
    if prefs.get("sidebar_width"):
        props.append(f"--sidebar-width:{prefs['sidebar_width']}px")
    if prefs.get("content_max_width"):
        props.append(f"--content-width:{prefs['content_max_width']}px")
    if prefs.get("contrast"):
        strength = 30 + prefs["contrast"] * 12
        props += [f"--border:color-mix(in srgb, var(--bw-text) {strength}%, transparent)",
                  f"--border-strong:color-mix(in srgb, var(--bw-text) {min(100, strength + 20)}%, transparent)"]
    if prefs.get("line_height") == 2:
        props.append("--line-height:2.1")
    rules = [f"html:root{{{';'.join(props)}}}"]
    if prefs.get("letter_spacing"):
        rules.append(f"body{{letter-spacing:{prefs['letter_spacing'] * 0.03:.2f}em;word-spacing:"
                     f"{prefs['letter_spacing'] * 0.08:.2f}em}}")
    for key, (selector, levels) in _SEMANTIC_CSS.items():
        level = prefs.get(key) or 0
        if level:
            rules.append(f"{selector}{{{levels[level - 1]}}}")
    if background_url:
        rules.append(f"body{{background:var(--bw-bg) url('{background_url}') center/cover fixed no-repeat}}"
                     ".main{background:color-mix(in srgb, var(--bw-bg) 88%, transparent)}")
    return "".join(rules)
