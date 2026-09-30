"""Colours, default theme, theme files and the site icon.

Custom icons are stored in the ``favicons`` folder under names starting with
``custom_`` (``templating.favicon_url`` only serves those). As in 1.4,
``favicon_order`` lists the uploaded icons in display order,
``favicon_type``/``favicon_custom`` hold the selection and
``favicon_enabled`` decides whether the selection is used at all.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql
from ... import settings, storage
from ...templating import FAVICON_PRESETS, HEX_COLOR, THEME_DEFAULTS

THEME_EXTENSIONS = (".bwtheme", ".json")
MAX_THEME_BYTES = 128 * 1024
MAX_ICON_BYTES = 1024 * 1024
MAX_CUSTOM_ICONS = 50
ICON_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
MODES = ("dark", "light")


class AppearanceError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def color_column(mode: str, name: str) -> str:
    return f"{'light_' if mode == 'light' else ''}{name}_color"


def color_columns() -> list[str]:
    return [color_column(mode, name) for mode in MODES for name in THEME_DEFAULTS[mode]]


def palettes() -> dict[str, dict[str, str]]:
    """Current colours per mode (invalid stored values show the default)."""
    stored = settings.load()
    result = {}
    for mode in MODES:
        result[mode] = {}
        for name, default in THEME_DEFAULTS[mode].items():
            value = str(stored.get(color_column(mode, name)) or "")
            result[mode][name] = value if HEX_COLOR.match(value) else default
    return result


def default_mode() -> str:
    return "light" if settings.get("default_theme_mode") == "light" else "dark"


def parse_colors(form: Any) -> dict[str, str]:
    """Colours and default mode from the appearance form; raise on anything invalid."""
    values: dict[str, str] = {}
    for mode in MODES:
        for name in THEME_DEFAULTS[mode]:
            column = color_column(mode, name)
            value = str(form.get(column) or "").strip()
            if not HEX_COLOR.match(value):
                raise AppearanceError("site_admin.appearance.error.color", name=column)
            values[column] = value.lower()
    mode = form.get("default_theme_mode")
    if mode not in MODES:
        raise AppearanceError("site_admin.appearance.error.mode")
    values["default_theme_mode"] = mode
    return values


def default_colors() -> dict[str, str]:
    return {color_column(mode, name): value for mode in MODES for name, value in THEME_DEFAULTS[mode].items()}


# ── Theme files (1.4 .bwtheme format) ────────────────────────────────────────


def theme_payload() -> dict[str, Any]:
    return {
        "_meta": {"type": "bananawiki-theme", "format": "bwtheme", "version": 1, "exported_at": now_sql()},
        "theme": {"default_theme_mode": default_mode(), **palettes()},
    }


def theme_filename() -> str:
    name = re.sub(r"[^a-z0-9]+", "-", settings.site_name().lower()).strip("-") or "bananawiki"
    return f"{name}-theme.bwtheme"


def parse_theme_file(upload: FileStorage | None) -> dict[str, str]:
    """Validate an uploaded theme file and return the settings it sets."""
    if upload is None or not upload.filename:
        raise AppearanceError("site_admin.appearance.error.theme_missing")
    if not upload.filename.lower().endswith(THEME_EXTENSIONS):
        raise AppearanceError("site_admin.appearance.error.theme_extension")
    raw = upload.stream.read(MAX_THEME_BYTES + 1)
    if len(raw) > MAX_THEME_BYTES:
        raise AppearanceError("site_admin.appearance.error.theme_size", limit_kb=MAX_THEME_BYTES // 1024)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AppearanceError("site_admin.appearance.error.theme_invalid") from None
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    theme = payload.get("theme") if isinstance(payload, dict) else None
    if (not isinstance(meta, dict) or not isinstance(theme, dict) or meta.get("type") != "bananawiki-theme"
            or meta.get("format") != "bwtheme"):
        raise AppearanceError("site_admin.appearance.error.theme_invalid")
    form: dict[str, Any] = {"default_theme_mode": str(theme.get("default_theme_mode", "dark")).strip().lower()}
    for mode in MODES:
        palette = theme.get(mode)
        if not isinstance(palette, dict):
            raise AppearanceError("site_admin.appearance.error.theme_invalid")
        for name in THEME_DEFAULTS[mode]:
            form[color_column(mode, name)] = str(palette.get(name, ""))
    return parse_colors(form)


# ── Site icon ────────────────────────────────────────────────────────────────


def _stored_order() -> list[str]:
    try:
        order = json.loads(settings.get("favicon_order") or "[]")
    except (TypeError, ValueError):
        return []
    return [name for name in order if isinstance(name, str) and name.startswith("custom_")] if isinstance(
        order, list) else []


def _exists(name: str) -> bool:
    return name.startswith("custom_") and storage.resolve("favicons", name) is not None


def custom_icons() -> list[str]:
    """Uploaded icons in display order (only those whose file exists)."""
    names = list(dict.fromkeys(name for name in _stored_order() if _exists(name)))
    current = settings.get("favicon_custom") or ""
    if settings.get("favicon_type") == "custom" and current not in names and _exists(current):
        names.append(current)
    return names


def selection() -> tuple[str, str]:
    """``(type, custom filename)`` of the icon in use."""
    kind = settings.get("favicon_type") or "yellow"
    custom = settings.get("favicon_custom") or ""
    if kind == "custom" and _exists(custom):
        return "custom", custom
    return (kind if kind in FAVICON_PRESETS else "yellow"), ""


def select(kind: str, custom: str = "") -> None:
    if kind in FAVICON_PRESETS:
        settings.update({"favicon_type": kind, "favicon_custom": "", "favicon_enabled": 1})
        return
    if kind == "custom" and custom in custom_icons():
        settings.update({"favicon_type": "custom", "favicon_custom": custom, "favicon_enabled": 1})
        return
    raise AppearanceError("site_admin.favicon.error.unknown")


def upload_icon(upload: FileStorage | None) -> str:
    """Store an uploaded icon, add it to the list and select it; return its name."""
    if len(custom_icons()) >= MAX_CUSTOM_ICONS:
        raise AppearanceError("site_admin.favicon.error.too_many", limit=MAX_CUSTOM_ICONS)
    try:
        stored = storage.save(upload, "favicons", allowed=ICON_EXTENSIONS, max_bytes=MAX_ICON_BYTES,
                              images_only=True)
    except storage.UploadError as error:
        raise AppearanceError(error.key, **error.values) from None
    folder = storage.folder_path("favicons")
    name = f"custom_{stored.filename}"
    os.replace(folder / stored.filename, folder / name)
    order = [*custom_icons(), name]
    settings.update({"favicon_order": json.dumps(order), "favicon_type": "custom", "favicon_custom": name,
                     "favicon_enabled": 1})
    return name


def delete_icon(name: str) -> bool:
    """Delete an uploaded icon; return True when it was the one in use (the default takes over)."""
    if name not in custom_icons():
        raise AppearanceError("site_admin.favicon.error.unknown")
    storage.delete("favicons", name)
    values: dict[str, Any] = {"favicon_order": json.dumps([n for n in custom_icons() if n != name])}
    was_selected = selection() == ("custom", name) or settings.get("favicon_custom") == name
    if was_selected:
        values.update({"favicon_type": "yellow", "favicon_custom": ""})
    settings.update(values)
    return was_selected


def reorder(order: list[Any]) -> None:
    """Save a new order; unknown names are ignored, missing ones keep their place at the end."""
    known = custom_icons()
    wanted = [name for name in order if isinstance(name, str) and name in known]
    wanted = list(dict.fromkeys(wanted))
    settings.update({"favicon_order": json.dumps(wanted + [name for name in known if name not in wanted])})


def move(name: str, step: int) -> None:
    names = custom_icons()
    if name not in names:
        raise AppearanceError("site_admin.favicon.error.unknown")
    index = names.index(name)
    target = min(max(index + step, 0), len(names) - 1)
    names.insert(target, names.pop(index))
    reorder(names)
