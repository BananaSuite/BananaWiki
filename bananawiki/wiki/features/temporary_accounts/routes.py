"""Administration of temporary pages, accounts, roles and timed visibility (1.4 URLs under /admin/temporary)."""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from ... import accounts, auth
from ...db import db
from ...i18n import t
from ...registry import feature_blueprint
from ...templating import from_local_input
from ..pages import service as pages
from . import service
from .service import TemporaryError

bp = feature_blueprint("temporary_accounts", "temporary_accounts", __name__, template_folder="templates")

PICKER_LIMIT = 2000


def _back():
    return redirect(url_for("temporary_accounts.overview"))


def _form_page() -> dict[str, Any] | None:
    slug = (request.form.get("page_slug") or "").strip()
    page = pages.get_by_slug(slug, with_content=False) if slug else pages.get(request.form.get("page_id"),
                                                                             with_content=False)
    if page is None:
        auth.flash_t("temporary_accounts.error.page_missing", "error")
    return page


def _form_user() -> dict[str, Any] | None:
    user = accounts.by_id(request.form.get("user_id")) or accounts.by_username(request.form.get("username"))
    if user is None:
        auth.flash_t("temporary_accounts.error.user_missing", "error")
    return user


def _options() -> dict[str, Any]:
    return {"expires": from_local_input(request.form.get("expires_at")),
            "show_countdown": request.form.get("show_countdown") == "1", "actor": auth.current_user()}


def _run(action, success_key: str, **values: Any):
    try:
        action()
    except TemporaryError as exc:
        flash(t(exc.key, **exc.values), "error")
    else:
        auth.flash_t(success_key, "success", **values)
    return _back()


@bp.get("/admin/temporary")
@auth.admin_required
def overview():
    return render_template(
        "temporary_accounts/admin.html", items=service.overview(),
        page_choices=db.all("SELECT slug, title FROM pages WHERE is_home = 0 ORDER BY title COLLATE NOCASE LIMIT ?",
                            (PICKER_LIMIT,)),
        user_choices=db.all("SELECT username, role FROM users WHERE role != 'owner' ORDER BY username COLLATE NOCASE "
                            "LIMIT ?", (PICKER_LIMIT,)),
    )


@bp.post("/admin/temporary/page")
@auth.admin_required
def schedule_page():
    page = _form_page()
    if page is None:
        return _back()
    return _run(lambda: service.schedule_page_deletion(page, **_options()), "temporary_accounts.page_scheduled",
                title=page["title"])


@bp.post("/admin/temporary/visibility")
@auth.admin_required
def schedule_visibility():
    page = _form_page()
    if page is None:
        return _back()
    hide = request.form.get("state") != "show"
    return _run(lambda: service.schedule_visibility(page, hide=hide, **_options()),
                "temporary_accounts.visibility_scheduled", title=page["title"])


@bp.post("/admin/temporary/user")
@auth.admin_required
def schedule_user():
    target = _form_user()
    if target is None:
        return _back()
    return _run(lambda: service.schedule_account_deletion(target, **_options()), "temporary_accounts.user_scheduled",
                user=target["username"])


@bp.post("/admin/temporary/role")
@auth.admin_required
def schedule_role():
    target = _form_user()
    if target is None:
        return _back()
    original = request.form.get("original_role", "")
    return _run(lambda: service.schedule_role_revert(target, original, **_options()),
                "temporary_accounts.role_scheduled", user=target["username"])


def _removed(kind: str, key: Any):
    if not service.remove(kind, key):
        abort(404)
    auth.flash_t("temporary_accounts.removed", "success")
    return _back()


@bp.post("/admin/temporary/page/<int:page_id>/remove")
@auth.admin_required
def remove_page(page_id: int):
    return _removed("page", page_id)


@bp.post("/admin/temporary/visibility/<int:page_id>/remove")
@auth.admin_required
def remove_visibility(page_id: int):
    return _removed("visibility", page_id)


@bp.post("/admin/temporary/user/<string:user_id>/remove")
@auth.admin_required
def remove_user(user_id: str):
    return _removed("user", user_id)


@bp.post("/admin/temporary/role/<string:user_id>/remove")
@auth.admin_required
def remove_role(user_id: str):
    return _removed("role", user_id)
