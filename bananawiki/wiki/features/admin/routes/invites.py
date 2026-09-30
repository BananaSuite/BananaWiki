"""Invite codes: /admin/codes…

Administrators see every code. Editors granted ``invite.view``,
``invite.generate`` or ``invite.delete`` reach these pages too, limited to
their own codes and to non-administrative roles.
"""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from .... import auth
from ....accounts import AccountError
from ....templating import from_local_input
from .. import invites
from .. import roles as custom_roles
from ..blueprint import bp
from .common import actor, refuse


def _allowed(*keys: str) -> bool:
    return any(auth.has_permission(key) for key in keys)


def _code_or_404(code_id: int) -> dict[str, Any]:
    code = invites.get_for(actor(), code_id)
    if code is None:
        abort(404)
    return code


def _page(template: str, codes: list[dict[str, Any]]):
    me = actor()
    return render_template(
        template,
        codes=codes,
        manages_all=invites.manages_all(me),
        can_generate=auth.has_permission("invite.generate"),
        can_delete=auth.has_permission("invite.delete"),
        assignable_roles=invites.assignable_roles(me),
        custom_roles=custom_roles.list_roles(),
        expiry_presets=invites.EXPIRY_PRESETS,
    )


@bp.get("/admin/codes")
def codes():
    if not _allowed("invite.view", "invite.generate"):
        return auth.deny()
    shown = invites.list_active(actor()) if auth.has_permission("invite.view") else []
    return _page("admin/codes.html", shown)


@bp.get("/admin/codes/expired")
@auth.permission_required("invite.view")
def codes_expired():
    return _page("admin/codes_expired.html", invites.list_expired(actor()))


@bp.post("/admin/codes/generate")
@auth.permission_required("invite.generate")
def generate_code():
    try:
        req = invites.parse_request(request.form, from_local_input(request.form.get("custom_expiry")))
        code = invites.generate(actor(), req)
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.codes.generated", "success", value=code)
    return redirect(url_for("admin.codes"))


@bp.post("/admin/codes/<int:code_id>/delete")
@auth.permission_required("invite.delete")
def delete_code(code_id: int):
    invites.deactivate(_code_or_404(code_id))
    auth.flash_t("admin.codes.deactivated", "success")
    return redirect(url_for("admin.codes"))


@bp.post("/admin/codes/expired/<int:code_id>/delete")
@auth.permission_required("invite.delete")
def purge_code(code_id: int):
    try:
        invites.purge(_code_or_404(code_id))
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.codes.purged", "success")
    return redirect(url_for("admin.codes_expired"))
