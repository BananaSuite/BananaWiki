"""Appearance views. The favicon URLs of 1.4 (``/admin/settings/favicon/...`` and
``/global-settings/...``) keep working and answer JSON to scripts."""

from __future__ import annotations

import io
import json
from typing import Any

from flask import jsonify, redirect, render_template, request, send_file, url_for

from ... import auth, settings
from ...i18n import t
from ...templating import FAVICON_PRESETS, THEME_DEFAULTS
from ..audit import record
from . import appearance
from .appearance import AppearanceError
from .blueprint import bp


def _page():
    return redirect(url_for("site_admin.appearance"))


def _answer(error: AppearanceError | None = None, message: str = "common.saved", **payload: Any):
    """JSON for scripts, a flash and a redirect for forms."""
    if auth.wants_json():
        if error:
            return jsonify({"ok": False, "error": t(error.key, **error.values)}), 400
        return jsonify({"ok": True, **payload})
    if error:
        auth.flash_t(error.key, "error", **error.values)
    else:
        auth.flash_t(message, "success")
    return _page()


@bp.get("/admin/appearance", endpoint="appearance")
@auth.admin_required
def appearance_page():
    kind, custom = appearance.selection()
    return render_template(
        "site_admin/appearance.html", palettes=appearance.palettes(), default_mode=appearance.default_mode(),
        defaults=THEME_DEFAULTS, presets=FAVICON_PRESETS, customs=appearance.custom_icons(),
        selected_type=kind, selected_custom=custom, favicon_enabled=bool(settings.get("favicon_enabled")),
        column=appearance.color_column,
    )


@bp.post("/admin/appearance/colors")
@auth.admin_required
def save_colors():
    try:
        values = appearance.parse_colors(request.form)
    except AppearanceError as error:
        return _answer(error)
    settings.update(values)
    record("appearance.updated")
    return _answer()


@bp.post("/admin/appearance/reset")
@auth.admin_required
def reset_colors():
    settings.update(appearance.default_colors())
    record("appearance.theme_reset")
    return _answer(message="site_admin.appearance.reset_done")


@bp.get("/admin/appearance/theme/export")
@bp.get("/global-settings/theme/export", endpoint="theme_export_legacy")
@auth.admin_required
def theme_export():
    body = json.dumps(appearance.theme_payload(), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    return send_file(io.BytesIO(body), mimetype="application/json", as_attachment=True,
                     download_name=appearance.theme_filename())


@bp.post("/admin/appearance/theme/import")
@bp.post("/global-settings/theme/import", endpoint="theme_import_legacy")
@auth.admin_required
def theme_import():
    try:
        values = appearance.parse_theme_file(request.files.get("theme_file"))
    except AppearanceError as error:
        return _answer(error)
    settings.update(values)
    record("appearance.theme_imported")
    return _answer(message="site_admin.appearance.theme_imported")


# ── Site icon ────────────────────────────────────────────────────────────────


def _legacy(rule: str, name: str):
    """Register a view under its 1.6 URL and both 1.4 URLs."""

    def decorate(view):
        bp.add_url_rule(f"/admin/appearance/favicon/{rule}", name, view, methods=["POST"])
        bp.add_url_rule(f"/admin/settings/favicon/{rule}", f"{name}_legacy", view, methods=["POST"])
        bp.add_url_rule(f"/global-settings/favicon/{rule}", f"{name}_legacy2", view, methods=["POST"])
        return view

    return decorate


def _data() -> dict[str, Any]:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


@auth.admin_required
def favicon_select():
    data = _data()
    kind = str(data.get("favicon_type") or "").strip()
    custom = str(data.get("favicon_custom") or "").strip()
    try:
        appearance.select(kind, custom)
    except AppearanceError as error:
        return _answer(error)
    record("favicon.selected", target_type="favicon", target_id=custom or kind)
    return _answer(message="site_admin.favicon.selected", type=kind, filename=custom)


@auth.admin_required
def favicon_upload():
    try:
        name = appearance.upload_icon(request.files.get("file"))
    except AppearanceError as error:
        return _answer(error)
    record("favicon.uploaded", target_type="favicon", target_id=name)
    return _answer(message="site_admin.favicon.uploaded", filename=name, type="custom", selected=True)


@auth.admin_required
def favicon_delete():
    name = str(_data().get("filename") or "").strip()
    try:
        was_selected = appearance.delete_icon(name)
    except AppearanceError as error:
        return _answer(error)
    record("favicon.deleted", target_type="favicon", target_id=name)
    return _answer(message="site_admin.favicon.deleted", deleted=name,
                   fallback_type="yellow" if was_selected else None)


@auth.admin_required
def favicon_reorder():
    data = _data()
    try:
        if "order" in data:
            if not isinstance(data["order"], list):
                raise AppearanceError("site_admin.favicon.error.order")
            appearance.reorder(data["order"])
        else:
            appearance.move(str(data.get("filename") or ""), -1 if data.get("direction") == "up" else 1)
    except AppearanceError as error:
        return _answer(error)
    return _answer(message="site_admin.favicon.reordered")


_legacy("select", "favicon_select")(favicon_select)
_legacy("upload", "favicon_upload")(favicon_upload)
_legacy("delete", "favicon_delete")(favicon_delete)
_legacy("reorder", "favicon_reorder")(favicon_reorder)


@bp.post("/admin/appearance/favicon/enabled")
@auth.admin_required
def favicon_enabled():
    enabled = 1 if request.form.get("favicon_enabled") == "1" else 0
    settings.update({"favicon_enabled": enabled})
    record("favicon.toggled", details={"enabled": enabled})
    return _answer()


@bp.post("/admin/settings/restore-favicon/<string:preset>")
@bp.post("/global-settings/restore-favicon/<string:preset>", endpoint="restore_favicon_legacy")
@auth.admin_required
def restore_favicon(preset: str):
    """1.4 regenerated a preset's image; 1.6 ships them read-only, so this selects it."""
    if preset not in FAVICON_PRESETS:
        return _answer(AppearanceError("site_admin.favicon.error.unknown"))
    appearance.select(preset)
    record("favicon.selected", target_type="favicon", target_id=preset)
    return _answer(message="site_admin.favicon.selected")
