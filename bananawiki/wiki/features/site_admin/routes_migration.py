"""Whole-site export and import views (``/admin/migration``).

Owners and superusers only: an archive holds every password hash and
private message, and an import replaces every account, so either would let a
plain administrator step outside the account hierarchy.
"""

from __future__ import annotations

import logging
import shutil

from flask import current_app, redirect, render_template, request, send_file, session, url_for

from ....core.passwords import verify_password
from ... import auth, i18n
from ..audit import record
from . import migration
from .blueprint import bp
from .migration import MigrationError

log = logging.getLogger("bananawiki.site_admin")
RATE_LIMIT = (5, 300)  # attempts per window (seconds), per administrator
MAX_LISTED_NAMES = 50  # accounts named in a notice or an audit entry


def _back():
    return redirect(url_for("site_admin.migration_page"))


def _may_move_site() -> bool:
    from ..admin.service import is_top

    return is_top(auth.real_user())


def _confirmed(action: str) -> bool:
    """The administrator re-entered their password (the archive holds every password hash)."""
    user = auth.current_user()
    limiter = current_app.extensions["bananawiki.limiter"]
    if not limiter.hit(f"site-migration:{user['id']}", *RATE_LIMIT):
        auth.flash_t("site_admin.migration.error.rate_limited", "error")
        return False
    if auth.is_impersonating():
        record(f"site.{action}_refused", details={"reason": "impersonating"})
        auth.flash_t("site_admin.migration.error.impersonating", "error")
        return False
    password = request.form.get("password", "")
    if not password or len(password) > 1024 or not verify_password(user.get("password"), password):
        record(f"site.{action}_refused", details={"reason": "wrong_password"})
        auth.flash_t("site_admin.migration.error.password", "error")
        return False
    return True


@bp.get("/admin/migration")
@auth.admin_required
def migration_page():
    cfg = current_app.config["BW"]
    return render_template("site_admin/migration.html", import_allowed=migration.import_allowed(),
                           limit_mb=cfg.max_import_size // (1024 * 1024), may_move_site=_may_move_site())


@bp.post("/admin/migration/export")
@auth.admin_required
def migration_export():
    if not _may_move_site():
        record("site.export_refused", details={"reason": "not_owner"})
        return auth.deny()
    if not _confirmed("export"):
        return _back()
    path, name = migration.export_archive()
    record("site.exported", details={"size": path.stat().st_size})
    # The open handle keeps the data readable after its folder is removed, so no
    # copy of the database stays on disk however the download ends.
    handle = open(path, "rb")  # noqa: SIM115 - closed by the response
    shutil.rmtree(path.parent, ignore_errors=True)
    return send_file(handle, mimetype="application/zip", as_attachment=True, download_name=name, max_age=0)


@bp.post("/admin/migration/import")
@auth.admin_required
def migration_import():
    if not _may_move_site():
        record("site.import_refused", details={"reason": "not_owner"})
        return auth.deny()
    if not migration.import_allowed():
        auth.flash_t("site_admin.migration.error.not_allowed", "error")
        return _back()
    if request.form.get("confirm_replace") != "1":
        auth.flash_t("site_admin.migration.error.confirm", "error")
        return _back()
    if not _confirmed("import"):
        return _back()
    try:
        staged = migration.stage(request.files.get("import_file"))
    except MigrationError as error:
        record("site.import_refused", details={"reason": error.key.rsplit(".", 1)[-1]})
        auth.flash_t(error.key, "error", **error.values)
        return _back()
    skipped, skipped_languages = staged.skipped_members, staged.skipped_languages
    two_step_off = staged.two_step_off
    try:
        backup = migration.apply(staged)
    except Exception:  # noqa: BLE001 - apply() already restored the previous site
        log.exception("Site import failed")
        auth.flash_t("site_admin.migration.error.failed", "error")
        return _back()
    record("site.imported", details={"backup": backup.name, "skipped_files": skipped})
    if two_step_off:
        record("site.import_two_step_off",
               details={"count": len(two_step_off), "usernames": two_step_off[:MAX_LISTED_NAMES]})
    language = session.get("interface_language")
    i18n.current_language()  # the notices below are written in the language of the page the import came from
    session.clear()
    if language:
        session["interface_language"] = language
    auth.flash_t("site_admin.migration.imported", "success", backup=backup.name)
    if skipped or skipped_languages:
        auth.flash_t("site_admin.migration.skipped", "warning", count=skipped + skipped_languages)
    if two_step_off:
        names = ", ".join(two_step_off[:MAX_LISTED_NAMES]) + (" …" if len(two_step_off) > MAX_LISTED_NAMES else "")
        auth.flash_t("site_admin.migration.two_step_off", "warning", count=len(two_step_off), names=names)
    return redirect(url_for("auth.login"))
