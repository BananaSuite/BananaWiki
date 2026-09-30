"""Custom roles: /admin/roles…"""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from .... import auth
from .... import permissions as perms
from ....accounts import AccountError
from ....features.pages import categories
from .. import roles as custom_roles
from .. import service
from ..blueprint import bp
from .common import actor, refuse


def _role_or_404(role_id: int) -> dict[str, Any]:
    role = custom_roles.get(role_id)
    if role is None:
        abort(404)
    return role


def _editor(role: dict[str, Any] | None, form: custom_roles.RoleForm | None = None):
    """Render the create/edit form, keeping submitted values after an error."""
    if form is not None:
        values = {"name": form.name, "description": form.description, "base_role": form.base_role,
                  "keys": frozenset(form.keys), "read_restricted": form.read_restricted,
                  "read_ids": frozenset(form.read_ids), "write_restricted": form.write_restricted,
                  "write_ids": frozenset(form.write_ids)}
    elif role is not None:
        values = role
    else:
        values = {"name": "", "description": "", "base_role": "user", "keys": perms.defaults("user"),
                  "read_restricted": False, "read_ids": frozenset(), "write_restricted": False,
                  "write_ids": frozenset()}
    editor_only = {key for key, p in perms.CATALOGUE.items() if p.editor_only}
    return render_template(
        "admin/role_edit.html",
        role=role,
        values=values,
        groups=perms.grouped_for("editor"),
        editor_only=editor_only,
        all_categories=categories.all_categories(),
        holders=custom_roles.holders(role["id"]) if role else [],
        candidates=custom_roles.assignable_users(role["id"]) if role else [],
        other_roles=[r for r in custom_roles.list_roles() if not role or r["id"] != role["id"]],
    )


@bp.get("/admin/roles")
@auth.admin_required
def roles():
    return render_template("admin/roles.html", roles=custom_roles.list_roles())


@bp.route("/admin/roles/create", methods=["GET", "POST"])
@auth.admin_required
def create_role():
    if request.method == "POST":
        form = custom_roles.RoleForm.from_form(request.form)
        try:
            role_id = custom_roles.create(form, actor_id=actor()["id"])
        except AccountError as error:
            refuse(error)
            return _editor(None, form), 400
        auth.flash_t("admin.roles.created", "success", name=form.name)
        return redirect(url_for("admin.edit_role", role_id=role_id))
    return _editor(None)


@bp.route("/admin/roles/<int:role_id>", methods=["GET", "POST"])
@auth.admin_required
def edit_role(role_id: int):
    role = _role_or_404(role_id)
    if request.method == "POST":
        form = custom_roles.RoleForm.from_form(request.form)
        try:
            custom_roles.update(role, form, actor_id=actor()["id"])
        except AccountError as error:
            refuse(error)
            return _editor(role, form), 400
        auth.flash_t("admin.roles.updated", "success")
        return redirect(url_for("admin.edit_role", role_id=role_id))
    return _editor(role)


def _member(role_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    role = _role_or_404(role_id)
    return role, service.get_user(request.form.get("user_id", ""))


@bp.post("/admin/roles/<int:role_id>/assign")
@auth.admin_required
def assign_role_user(role_id: int):
    role, target = _member(role_id)
    try:
        if target is None:
            raise AccountError("admin.users.error.not_found")
        service.assign_custom_role(actor(), target, role)
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.roles.assigned", "success", username=target["username"], name=role["name"])
    return redirect(url_for("admin.edit_role", role_id=role_id))


@bp.post("/admin/roles/<int:role_id>/unassign")
@auth.admin_required
def unassign_role_user(role_id: int):
    role, target = _member(role_id)
    try:
        if target is None or target.get("custom_role_id") != role["id"]:
            raise AccountError("admin.roles.error.not_assigned")
        service.remove_custom_role(actor(), target)
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.roles.unassigned", "success", username=target["username"], name=role["name"])
    return redirect(url_for("admin.edit_role", role_id=role_id))


@bp.post("/admin/roles/<int:role_id>/delete")
@auth.admin_required
def delete_role(role_id: int):
    role = _role_or_404(role_id)
    try:
        moved_to = custom_roles.delete(role, request.form.get("replacement_role_id") or "auto",
                                       actor_id=actor()["id"])
    except AccountError as error:
        refuse(error)
        return redirect(url_for("admin.edit_role", role_id=role_id))
    if moved_to:
        auth.flash_t("admin.roles.deleted_moved", "success", name=role["name"], target=moved_to["name"])
    else:
        auth.flash_t("admin.roles.deleted", "success", name=role["name"])
    return redirect(url_for("admin.roles"))
