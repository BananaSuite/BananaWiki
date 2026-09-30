"""Server page: restart (with cooldown) and the error log."""

from __future__ import annotations

import logging

from flask import redirect, render_template, request, url_for

from ... import auth
from ..audit import record
from . import server
from .blueprint import bp

log = logging.getLogger("bananawiki.site_admin")


@bp.get("/admin/server")
@auth.admin_required
def server_page():
    return render_template("site_admin/server.html", can_restart=bool(server.gunicorn_master_pid()),
                           cooldown=server.cooldown_remaining(), cooldown_seconds=server.COOLDOWN_SECONDS)


@bp.post("/admin/server/restart")
@bp.post("/admin/settings/restart-server", endpoint="restart_legacy")
@bp.post("/global-settings/restart-server", endpoint="restart_legacy2")
@auth.admin_required
def restart():
    back = redirect(url_for("site_admin.server_page"))
    if request.form.get("confirm") != "1":
        auth.flash_t("site_admin.server.confirm_required", "error")
        return back
    master = server.gunicorn_master_pid()
    if not master:
        auth.flash_t("site_admin.server.not_gunicorn", "error")
        return back
    if not server.claim_restart():
        auth.flash_t("site_admin.server.cooldown", "error", seconds=max(1, server.cooldown_remaining()))
        return back
    record("server.restart", details={"master_pid": master})
    try:
        server.restart(master)
    except OSError as error:
        log.error("Could not signal the Gunicorn master %s: %s", master, error)
        auth.flash_t("site_admin.server.failed", "error")
        return back
    auth.flash_t("site_admin.server.restarting", "success")
    return back


@bp.get("/admin/error-log")
@auth.admin_required
def error_log():
    errors_only = request.args.get("level", "error") != "all"
    return render_template("site_admin/error_log.html", content=server.log_tail(errors_only),
                           errors_only=errors_only)
