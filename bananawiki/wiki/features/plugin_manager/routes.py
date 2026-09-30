"""Admin pages: /admin/plugins (the 1.4 URLs) for features, third-party plugins and the sidebar order."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, url_for

from ... import auth, plugins_external
from ...plugins_external import ManifestError
from . import archive, service

bp = Blueprint("plugin_manager", __name__, template_folder="templates", static_folder="static",
               static_url_path="/static/plugin_manager")

log = logging.getLogger("bananawiki.plugins")

RATE_LIMITS = {"toggle": 20, "install": 5, "delete": 10, "restart": 2, "order": 30}


def _rate_limited(action: str) -> bool:
    user = auth.real_user() or {}
    limiter = current_app.extensions["bananawiki.limiter"]
    return not limiter.hit(f"plugins:{action}:{user.get('id')}", RATE_LIMITS[action], 60)


def _back():
    return redirect(url_for("plugin_manager.index"))


def _entry_or_404(plugin_id: str) -> service.Entry:
    entry = service.get(plugin_id)
    if entry is None or entry.easy_wiki_hidden:
        abort(404)
    return entry


def _flash_error(error: ManifestError) -> None:
    auth.flash_t(error.key, "error", **error.values)


def _password(action: str, plugin_id: str) -> str:
    """``ok``, ``missing`` or ``wrong`` for the password re-entered on a confirmation form."""
    password = request.form.get("password", "")
    if not password:
        return "missing"
    if service.password_ok(password):
        return "ok"
    user = auth.real_user() or {}
    log.warning("plugin_password_rejected action=%s plugin=%s user=%s", action, plugin_id, user.get("username"))
    auth.flash_t("plugin_manager.flash.wrong_password", "error")
    return "wrong"


def _audit(action: str, plugin_id: str, **details: object) -> None:
    user = auth.real_user() or {}
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    log.info("%s plugin=%s user=%s %s", action, plugin_id, user.get("username"), extra)


# ── Pages ────────────────────────────────────────────────────────────────────


@bp.get("/admin/plugins")
@auth.admin_required
def index():
    entries = service.entries()
    rt = plugins_external.runtime()
    return render_template(
        "plugin_manager/plugins.html",
        features=[e for e in entries if e.kind == "feature"],
        externals=[e for e in entries if e.kind == "external"],
        retired=[e for e in entries if e.kind == "retired"],
        invalid=list(rt.invalid.values()),
        pending=[e for e in entries if e.restart],
        restart_mode=service.restart_mode(),
        external_allowed=rt.allowed,
        managed=current_app.config["BW"].managed_hosting,
        apps=service.sidebar_apps(),
        max_archive_mb=archive.MAX_ARCHIVE_BYTES // archive.MIB,
    )


@bp.get("/admin/plugins/<plugin_id>")
@auth.admin_required
def detail(plugin_id: str):
    entry = _entry_or_404(plugin_id)
    return render_template(
        "plugin_manager/plugin_detail.html", entry=entry,
        requires=service.names(entry.requires), required_by=service.names(entry.required_by),
        data_tables=archive.data_tables(entry.id) if entry.kind == "external" else [],
        restart_mode=service.restart_mode(),
    )


# ── Switching ────────────────────────────────────────────────────────────────


@bp.post("/admin/plugins/<plugin_id>/enable")
@auth.admin_required
def enable(plugin_id: str):
    entry = _entry_or_404(plugin_id)
    if _rate_limited("toggle"):
        return auth.deny(429, "error.429.title")
    if not entry.can_enable:
        auth.flash_t(entry.lock or entry.error_key or "plugin_manager.flash.not_switchable", "error",
                     **entry.error_values)
        return _back()
    missing = service.missing_requirements(entry)
    enable_deps = request.form.get("enable_deps") == "1"
    force = request.form.get("force") == "1"
    if missing and not (enable_deps or force):
        return render_template("plugin_manager/plugin_enable_confirm.html", entry=entry,
                               missing=service.names(missing))
    dependencies = [service.get(dep) for dep in missing] if enable_deps else []
    for dependency in dependencies:
        if dependency is None or not dependency.can_enable:
            auth.flash_t("plugin_manager.flash.dependency_unavailable", "error", id=dependency.id if dependency else "")
            return _back()
    external = [e for e in (*dependencies, entry) if e and e.kind == "external"]
    if external:
        status = _password("enable", entry.id)
        if status != "ok":
            return render_template("plugin_manager/plugin_enable_external.html", entry=entry,
                                   external=[(e.id, e.name) for e in external],
                                   enable_deps=enable_deps, force=force)
        try:
            snapshot = service.backup_database(entry.id)
        except Exception:  # noqa: BLE001 - no way back means no enabling
            log.exception("Backup before enabling plugin %s failed", entry.id)
            auth.flash_t("plugin_manager.flash.backup_failed", "error")
            return _back()
        _audit("plugin_code_trusted", entry.id, snapshot=snapshot.name)
    for item in (*dependencies, entry):
        if item is not None:
            service.set_state(item, True)
            _audit("plugin_enabled", item.id, kind=item.kind)
    if external:
        auth.flash_t("plugin_manager.flash.enabled_restart", "success", name=entry.name)
    else:
        auth.flash_t("plugin_manager.flash.enabled", "success", name=entry.name)
    if missing and force:
        auth.flash_t("plugin_manager.flash.enabled_missing", "warning", deps=", ".join(missing))
    return _back()


@bp.post("/admin/plugins/<plugin_id>/disable")
@auth.admin_required
def disable(plugin_id: str):
    entry = _entry_or_404(plugin_id)
    if _rate_limited("toggle"):
        return auth.deny(429, "error.429.title")
    if not entry.can_disable:
        auth.flash_t(entry.lock or "plugin_manager.flash.not_switchable", "error")
        return _back()
    dependents = service.enabled_dependents(entry)
    cascade = request.form.get("cascade") == "1"
    if dependents and not (cascade or request.form.get("force") == "1"):
        return render_template("plugin_manager/plugin_disable_confirm.html", entry=entry,
                               dependents=service.names(dependents))
    targets = [service.get(dep) for dep in dependents] if cascade else []
    for item in (*targets, entry):
        if item is not None and item.can_disable:
            service.set_state(item, False)
            _audit("plugin_disabled", item.id, kind=item.kind)
            auth.flash_t("plugin_manager.flash.disabled", "success", name=item.name)
    return _back()


# ── Installing and removing ──────────────────────────────────────────────────


@bp.post("/admin/plugins/import")
@auth.admin_required
def import_plugin():
    if not plugins_external.runtime().allowed:
        auth.flash_t("plugin_manager.lock.external_off", "error")
        return _back()
    if _rate_limited("install"):
        return auth.deny(429, "error.429.title")
    upload = request.files.get("plugin_file")
    if upload is None or not upload.filename:
        auth.flash_t("plugin_manager.flash.no_file", "error")
        return _back()
    if not upload.filename.lower().endswith(".bwplugin"):
        auth.flash_t("plugin_manager.flash.not_bwplugin", "error")
        return _back()
    status = _password("install", upload.filename)
    if status != "ok":
        if status == "missing":
            auth.flash_t("plugin_manager.flash.password_required", "error")
        return _back()
    with tempfile.TemporaryDirectory(prefix="bw-plugin-upload-") as folder:
        path = Path(folder) / "upload.bwplugin"
        upload.save(path)
        try:
            manifest = archive.install(path)
        except ManifestError as error:
            _flash_error(error)
            return _back()
    _audit("plugin_imported", manifest.id, version=manifest.version)
    auth.flash_t("plugin_manager.flash.imported", "success", name=manifest.name)
    return redirect(url_for("plugin_manager.detail", plugin_id=manifest.id))


@bp.post("/admin/plugins/<plugin_id>/delete")
@auth.admin_required
def delete(plugin_id: str):
    entry = _entry_or_404(plugin_id)
    if _rate_limited("delete"):
        return auth.deny(429, "error.429.title")
    if not entry.removable:
        auth.flash_t("plugin_manager.flash.builtin_not_removable", "error")
        return _back()
    if entry.kind == "retired" or not entry.has_files:
        service.remove_row(entry.id)
        _audit("plugin_row_removed", entry.id)
        auth.flash_t("plugin_manager.flash.row_removed", "success", name=entry.name)
        return _back()
    tables = archive.data_tables(entry.id)
    if _password("delete", entry.id) != "ok":
        return render_template("plugin_manager/plugin_delete_confirm.html", entry=entry, data_tables=tables,
                               dependents=service.names(service.enabled_dependents(entry)))
    drop = [name for name in request.form.getlist("data_table") if name in tables] \
        if request.form.get("drop_data") == "1" else []
    if drop:
        try:
            service.backup_database(entry.id)
        except Exception:  # noqa: BLE001
            log.exception("Backup before dropping the data of plugin %s failed", entry.id)
            auth.flash_t("plugin_manager.flash.backup_failed_drop", "error")
            return _back()
    if entry.loaded:
        plugins_external.runtime().notify(entry.id, "disable")
    dropped = archive.uninstall(entry.id, drop)
    _audit("plugin_deleted", entry.id, dropped_tables=",".join(dropped))
    auth.flash_t("plugin_manager.flash.deleted", "success", name=entry.name)
    return _back()


@bp.get("/admin/plugins/sdk-download")
@auth.admin_required
def sdk_download():
    return Response(archive.sdk_bundle(), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="bananawiki-plugin-kit.zip"'})


# ── Sidebar order and restart ────────────────────────────────────────────────


@bp.post("/admin/plugins/order")
@auth.admin_required
def save_order():
    if _rate_limited("order"):
        return auth.deny(429, "error.429.title")
    ids = [value for value in request.form.getlist("order") if value]
    moving = request.form.get("move", "")
    if ":" in moving:
        plugin_id, direction = moving.rsplit(":", 1)
        ids = service.move(ids, plugin_id, direction)
    service.save_sidebar_order(ids)
    if auth.wants_json():
        return {"ok": True}
    auth.flash_t("plugin_manager.flash.order_saved", "success")
    return redirect(url_for("plugin_manager.index") + "#sidebar-order")


@bp.post("/admin/plugins/restart")
@auth.admin_required
def restart():
    if _rate_limited("restart"):
        return auth.deny(429, "error.429.title")
    if service.request_restart():
        _audit("server_restart", "-")
        auth.flash_t("plugin_manager.flash.restarting", "success")
    else:
        auth.flash_t(f"plugin_manager.restart.{service.restart_mode()}", "warning")
    return _back()


def _is_admin(user: dict | None) -> bool:
    return bool(user) and auth.is_admin(user)


def pending_restarts(user: dict | None) -> int:
    """Plugin changes waiting for a restart (admin menu badge)."""
    if not _is_admin(user) or not plugins_external.runtime().plugins:
        return 0
    return sum(1 for entry in service.entries() if entry.restart)
