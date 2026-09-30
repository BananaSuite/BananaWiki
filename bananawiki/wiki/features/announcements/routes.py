"""Routes: announcement administration and the public announcement page (1.4 URLs)."""

from __future__ import annotations

from flask import abort, redirect, render_template, request, url_for
from markupsafe import Markup

from ... import auth
from ...i18n import t
from ...markdown import render as render_markdown
from ...registry import feature_blueprint
from . import service

bp = feature_blueprint("announcements", "announcements", __name__, template_folder="templates",
                       static_folder="static", static_url_path="/static/announcements")


def _errors(data: service.AnnouncementInput) -> list[str]:
    return [t(key, **values) for key, values in data.errors]


def _render_list(form: service.AnnouncementInput, errors: list[str] | None = None, status: int = 200):
    return render_template("announcements/admin.html", announcements=service.list_all(), form=form,
                           errors=errors or [], service=service), status


def _announcement(announcement_id: int) -> dict:
    row = service.get(announcement_id)
    if row is None:
        abort(404)
    return row


@bp.get("/admin/announcements")
@auth.admin_required
def admin():
    return _render_list(service.AnnouncementInput())


@bp.post("/admin/announcements/create")
@auth.admin_required
def create():
    data = service.parse_form(request.form, editing=False)
    if data.errors:
        return _render_list(data, _errors(data), 400)
    service.create(data, actor_id=auth.current_user()["id"])
    auth.flash_t("announcements.created", "success")
    return redirect(url_for("announcements.admin"))


@bp.route("/admin/announcements/<int:announcement_id>/edit", methods=["GET", "POST"])
@auth.admin_required
def edit(announcement_id: int):
    row = _announcement(announcement_id)
    if request.method == "GET":
        return render_template("announcements/edit.html", announcement=row, form=service.as_input(row),
                               errors=[], service=service)
    data = service.parse_form(request.form, editing=True)
    if data.errors:
        return render_template("announcements/edit.html", announcement=row, form=data, errors=_errors(data),
                               service=service), 400
    service.update(announcement_id, data)
    auth.flash_t("announcements.updated", "success")
    return redirect(url_for("announcements.admin"))


@bp.post("/admin/announcements/<int:announcement_id>/delete")
@auth.admin_required
def delete(announcement_id: int):
    _announcement(announcement_id)
    service.delete(announcement_id)
    auth.flash_t("announcements.deleted", "success")
    return redirect(url_for("announcements.admin"))


@bp.get("/announcements/<int:announcement_id>")
@auth.public_read
def view(announcement_id: int):
    """One announcement in full, for whoever the banner is shown to."""
    row = service.visible_one(announcement_id, auth.current_user())
    if row is None:
        abort(404)
    return render_template("announcements/view.html", announcement=row,
                           content_html=Markup(render_markdown(row["content"])), style=service.custom_style(row))
