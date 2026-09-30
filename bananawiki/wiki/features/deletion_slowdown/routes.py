"""Administration of pages waiting to be deleted (1.4 URLs under /admin/pending-deletions)."""

from __future__ import annotations

from flask import abort, redirect, render_template, request, url_for

from ... import auth, settings
from ...registry import feature_blueprint
from ..pages import service as pages
from . import service

bp = feature_blueprint("deletion_slowdown", "deletion_slowdown", __name__, template_folder="templates")


@bp.get("/admin/pending-deletions")
@auth.admin_required
def pending():
    return render_template("deletion_slowdown/pending.html", pending=service.list_pending(),
                           grace_hours=service.GRACE_HOURS,
                           docs_bypass=bool(settings.get("docs_bypass_deletion_slowdown")))


@bp.post("/admin/pending-deletions/<int:page_id>/restore")
def restore(page_id: int):
    """Administrators, and anyone who may delete the page, can take the deletion back."""
    user = auth.current_user()
    page = pages.get(page_id, with_content=False)
    if page is None or not page["pending_deletion"]:
        abort(404)
    if not pages.can_delete(page, user):
        return auth.deny()
    service.restore(page, user["id"])
    auth.flash_t("deletion_slowdown.restored", "success", title=page["title"])
    return redirect(url_for("pages.view", slug=page["slug"]))


@bp.post("/admin/pending-deletions/<int:page_id>/purge")
@auth.admin_required
def purge(page_id: int):
    page = pages.get(page_id, with_content=False)
    if page is None or not page["pending_deletion"]:
        abort(404)
    service.purge(page, auth.current_user()["id"])
    auth.flash_t("deletion_slowdown.purged", "success", title=page["title"])
    return redirect(url_for("deletion_slowdown.pending"))


@bp.post("/admin/pending-deletions/settings")
@auth.admin_required
def save_settings():
    settings.update({"docs_bypass_deletion_slowdown": 1 if request.form.get("docs_bypass_deletion_slowdown") == "1"
                     else 0})
    auth.flash_t("common.saved", "success")
    return redirect(url_for("deletion_slowdown.pending"))
