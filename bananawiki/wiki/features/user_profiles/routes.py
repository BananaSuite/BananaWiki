"""Profile fields and publication for account owners; field, tag and profile moderation for admins."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from ... import accounts, auth, storage
from ...registry import feature_blueprint
from ..users import service as users
from . import service

bp = feature_blueprint("user_profiles", "user_profiles", __name__, template_folder="templates")


def _flash_error(error: Exception) -> None:
    auth.flash_t(getattr(error, "key", "error.generic.body"), "error", **getattr(error, "values", {}))


def _target(user_id: str) -> dict[str, Any]:
    target = accounts.by_id(user_id)
    if target is None:
        abort(404)
    if not service.can_moderate(target):
        abort(403)
    return target


def _fields_form(target: dict[str, Any], action: str, back: str):
    return render_template(
        "user_profiles/fields.html", target=target, action=action, back=back,
        definitions=service.definitions(), values=service.values(target["id"]),
    )


# ── Account owners ───────────────────────────────────────────────────────────


@bp.route("/settings/profile-fields", methods=["GET", "POST"])
@auth.permission_required("profile.edit_own")
def own_fields():
    user = auth.current_user()
    if request.method == "POST":
        try:
            service.save_values(user["id"], request.form)
        except service.ProfileFieldError as error:
            _flash_error(error)
        else:
            auth.flash_t("profiles.flash.fields_saved", "success")
        return redirect(url_for("user_profiles.own_fields"))
    return _fields_form(user, url_for("user_profiles.own_fields"), url_for("users.my_profile"))


@bp.post("/settings/profile/publish")
@auth.permission_required("profile.edit_own")
def publish():
    published = request.form.get("published") == "1"
    try:
        service.set_published(auth.current_user()["id"], published)
    except service.ProfileFieldError as error:
        _flash_error(error)
    else:
        auth.flash_t("profiles.flash.published" if published else "profiles.flash.hidden", "success")
    return redirect(url_for("users.settings"))


@bp.post("/settings/profile/delete")
@auth.permission_required("profile.edit_own")
def delete_own():
    users.delete_profile(auth.current_user()["id"])
    auth.flash_t("profiles.flash.deleted", "success")
    return redirect(url_for("users.settings"))


# ── Administrators: one account ──────────────────────────────────────────────


@bp.route("/admin/users/<user_id>/profile", methods=["GET", "POST"])
@auth.admin_required
def admin_profile(user_id: str):
    target = _target(user_id)
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "edit_profile":
                users.save_basics(target["id"], real_name=request.form.get("real_name"),
                                  bio=request.form.get("bio"), birth_date=request.form.get("birth_date"))
                upload = request.files.get("avatar")
                if upload is not None and upload.filename:
                    users.save_avatar(target["id"], upload)
            elif action == "remove_avatar":
                users.remove_avatar(target["id"])
            else:
                service.moderate(target["id"], action.removesuffix("_profile"))
        except (users.ProfileError, service.ProfileFieldError, storage.UploadError) as error:
            _flash_error(error)
        else:
            auth.flash_t("profiles.flash.moderated", "success")
        return redirect(url_for("user_profiles.admin_profile", user_id=target["id"]))
    record = users.get_profile(target["id"]) or {}
    return render_template("user_profiles/admin_profile.html", target=target, profile=record,
                           avatar_url=users.upload_url(record.get("avatar_filename")),
                           tags=service.tags(target["id"]))


@bp.route("/admin/users/<user_id>/profile-fields", methods=["GET", "POST"])
@auth.admin_required
def admin_fields(user_id: str):
    target = _target(user_id)
    if request.method == "POST":
        try:
            service.save_values(target["id"], request.form)
        except service.ProfileFieldError as error:
            _flash_error(error)
        else:
            auth.flash_t("profiles.flash.fields_saved", "success")
        return redirect(url_for("user_profiles.admin_fields", user_id=target["id"]))
    return _fields_form(target, url_for("user_profiles.admin_fields", user_id=target["id"]),
                        url_for("user_profiles.admin_profile", user_id=target["id"]))


@bp.post("/admin/users/<user_id>/tags")
@auth.admin_required
def admin_tags(user_id: str):
    target = _target(user_id)
    form = request.form
    action = form.get("action", "")
    tag_id = form.get("tag_id", type=int) or 0
    try:
        if action == "add_tag":
            service.add_tag(target["id"], form.get("tag_label"), form.get("tag_color"))
        elif action == "update_tag":
            service.update_tag(target["id"], tag_id, form.get("tag_label"), form.get("tag_color"))
        elif action == "delete_tag":
            service.delete_tag(target["id"], tag_id)
        elif action in ("move_up", "move_down"):
            service.move_tag(target["id"], tag_id, -1 if action == "move_up" else 1)
        else:
            abort(400)
    except service.ProfileFieldError as error:
        _flash_error(error)
    else:
        auth.flash_t("profiles.flash.tags_saved", "success")
    return redirect(url_for("user_profiles.admin_profile", user_id=target["id"]))


# ── Administrators: field definitions ────────────────────────────────────────


@bp.route("/admin/profile-fields", methods=["GET", "POST"])
@auth.admin_required
def admin_definitions():
    if request.method == "POST":
        form = request.form
        action = form.get("action", "create")
        field_id = form.get("field_id", type=int) or 0
        try:
            if action == "create":
                service.create_definition(form.get("label"), form.get("field_type"))
            elif service.get_definition(field_id) is None:
                abort(404)
            elif action == "update":
                service.update_definition(field_id, form.get("label"), form.get("field_type"))
            elif action == "delete":
                service.delete_definition(field_id)
            elif action in ("move_up", "move_down"):
                service.move_definition(field_id, -1 if action == "move_up" else 1)
            else:
                abort(400)
        except service.ProfileFieldError as error:
            _flash_error(error)
        else:
            auth.flash_t("profiles.flash.definitions_saved", "success")
        return redirect(url_for("user_profiles.admin_definitions"))
    return render_template("user_profiles/admin_definitions.html", definitions=service.definitions(),
                           types=service.FIELD_TYPES)
