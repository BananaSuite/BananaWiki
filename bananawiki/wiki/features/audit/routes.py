"""The audit log page (administrators only)."""

from __future__ import annotations

from flask import redirect, render_template, request, url_for

from ... import auth, registry, settings
from . import service

bp = registry.feature_blueprint("audit", "audit", __name__, template_folder="templates")


@bp.get("/admin/audit")
@auth.admin_required
def index():
    action = (request.args.get("action") or "").strip()[:100]
    actor = (request.args.get("actor") or "").strip()[:100]
    page_arg = request.args.get("page", "1")
    rows, page, pages = service.entries(action=action, actor=actor, page=int(page_arg) if page_arg.isdigit() else 1)
    return render_template(
        "audit/index.html", rows=rows, page=page, pages=pages, action=action, actor=actor,
        actions=service.actions(), retention_days=service.retention_days(), bounds=service.RETENTION_BOUNDS,
    )


@bp.post("/admin/audit/retention")
@auth.admin_required
def retention():
    days = request.form.get("audit_log_retention_days", type=int)
    low, high = service.RETENTION_BOUNDS
    if days is None or not low <= days <= high:
        auth.flash_t("audit.retention.invalid", "error", minimum=low, maximum=high)
        return redirect(url_for("audit.index"))
    previous = service.retention_days()
    settings.update({"audit_log_retention_days": days})
    service.record("audit.retention_changed", details={"old": previous, "new": days})
    auth.flash_t("common.saved", "success")
    return redirect(url_for("audit.index"))
