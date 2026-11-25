"""Asking for edit access from a page, and deciding under Admin → People → Access requests."""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from ... import accounts, auth, settings
from ...i18n import t
from ...registry import feature_blueprint
from ...templating import format_datetime
from ..audit import record_settings
from ..pages.blueprint import page_url, rate_limited, visible_page_or_404
from . import service
from .service import RequestError

bp = feature_blueprint("access_requests", "access_requests", __name__, template_folder="templates")


def _flash(error: RequestError) -> None:
    values = dict(error.values)
    if "until" in values:
        values["until"] = format_datetime(values["until"])
    flash(t(error.key, **values), "error")


@bp.post("/page/<slug>/ask-edit-access")
@rate_limited("ask-edit-access", 5)
def ask(slug: str):
    page = visible_page_or_404(slug)
    user = auth.current_user()
    if user is None:
        return auth.redirect_to_login()
    try:
        service.create(user, page, request.form.get("message", ""))
    except RequestError as error:
        _flash(error)
    else:
        auth.flash_t("access_requests.sent", "success")
    return redirect(page_url(page))


@bp.route("/admin/access-requests", methods=["GET", "POST"])
@auth.admin_required
def admin():
    if request.method == "POST":
        values = {service.SETTING: 1 if request.form.get(service.SETTING) == "1" else 0}
        previous = settings.load()
        settings.update(values)
        record_settings(previous, values, via="access_requests")
        auth.flash_t("common.saved", "success")
        return redirect(url_for("access_requests.admin"))
    requests = service.pending()
    for row in requests:
        row["changes"] = _changes(row)
    return render_template("access_requests/admin.html", requests=requests, decided=service.recent_decisions(),
                           enabled=service.enabled())


def _changes(row: dict[str, Any]) -> dict[str, list[Any]]:
    """What approving would change for the person now (they may have been given access since)."""
    target = accounts.by_id(row["user_id"])
    return service.changes_for(target, service.Scope(row["scope"], row["category_id"])) if target else {}


@bp.post("/admin/access-requests/<int:request_id>/<action>")
@auth.admin_required
def decide(request_id: int, action: str):
    if action not in ("approve", "decline"):
        abort(404)
    row = service.get(request_id)
    try:
        if action == "approve":
            service.approve(auth.current_user(), request_id)
        else:
            service.decline(auth.current_user(), request_id)
    except RequestError as error:
        _flash(error)
    else:
        auth.flash_t(f"access_requests.flash.{action}", "success", username=row["username"] if row else "")
    return redirect(url_for("access_requests.admin"))
