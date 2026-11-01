"""Profile fields and publication for account owners; field, tag and profile moderation for admins."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from ... import accounts, auth, storage
from ...registry import feature_blueprint
from ..audit.service import record as audit_record
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


def _audit(target: dict[str, Any], action: str, changed: list[str] | None = None) -> None:
    """Record an administrator's change to *target*'s public profile: what was done and which parts
    changed, never the values (the profile shows the member as their author)."""
    details: dict[str, Any] = {"username": target["username"], "action": action}
    if changed is not None:
        details["changed"] = ", ".join(changed)
    audit_record("user.profile_moderated", target_type="user", target_id=target["id"], details=details)


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The profile parts that differ (a missing profile has them all empty)."""
    return [name for name in users.PROFILE_FIELDS if (before.get(name) or None) != (after.get(name) or None)]


def _fields_form(target: dict[str, Any], action: str, back: str, *, admin_view: bool = False):
    return render_template(
        "user_profiles/fields.html", target=target, action=action, back=back, admin_view=admin_view,
        definitions=service.definitions(), values=service.values(target["id"]),
    )


# Account owners


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
    service.delete_own(auth.current_user()["id"])
    auth.flash_t("profiles.flash.deleted", "success")
    return redirect(url_for("users.settings"))


# Administrators: one account


@bp.route("/admin/users/<user_id>/profile", methods=["GET", "POST"])
@auth.admin_required
def admin_profile(user_id: str):
    target = _target(user_id)
    if request.method == "POST":
        action = request.form.get("action", "")
        before = users.get_profile(target["id"]) or {}
        failed = True
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
            failed = False
        except (users.ProfileError, service.ProfileFieldError, storage.UploadError) as error:
            _flash_error(error)
        finally:
            # The name and biography are saved before the avatar: a refused avatar (or any later error)
            # leaves them changed, and that is recorded too.
            changed = _changed(before, users.get_profile(target["id"]) or {})
            if not failed or changed:
                _audit(target, action, changed)
        if not failed:
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
        before = service.values(target["id"])
        try:
            service.save_values(target["id"], request.form)
        except service.ProfileFieldError as error:
            _flash_error(error)
        else:
            after = service.values(target["id"])
            labels = {field["id"]: field["label"] for field in service.definitions()}
            changed = [labels.get(field_id, str(field_id)) for field_id in sorted(set(before) | set(after))
                       if {k: (before.get(field_id) or {}).get(k) for k in ("value", "visible")}
                       != {k: (after.get(field_id) or {}).get(k) for k in ("value", "visible")}]
            _audit(target, "profile_fields", changed)
            auth.flash_t("profiles.flash.fields_saved", "success")
        return redirect(url_for("user_profiles.admin_fields", user_id=target["id"]))
    return _fields_form(target, url_for("user_profiles.admin_fields", user_id=target["id"]),
                        url_for("user_profiles.admin_profile", user_id=target["id"]), admin_view=True)


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
        _audit(target, action)
        auth.flash_t("profiles.flash.tags_saved", "success")
    return redirect(url_for("user_profiles.admin_profile", user_id=target["id"]))


# Administrators: field definitions


def _audit_definition(action: str, definition: dict[str, Any], **details: Any) -> None:
    """Record a field definition change: every public profile shows these fields."""
    audit_record(action, target_type="profile_field", target_id=definition["id"],
                 details={"key": definition["key"], "label": definition["label"],
                          "field_type": definition["field_type"], **details})


@bp.route("/admin/profile-fields", methods=["GET", "POST"])
@auth.admin_required
def admin_definitions():
    if request.method == "POST":
        form = request.form
        action = form.get("action", "create")
        field_id = form.get("field_id", type=int) or 0
        try:
            if action == "create":
                created = service.create_definition(form.get("label"), form.get("field_type"))
                _audit_definition("profile_field.created", service.get_definition(created))
            elif (previous := service.get_definition(field_id)) is None:
                abort(404)
            elif action == "update":
                service.update_definition(field_id, form.get("label"), form.get("field_type"))
                current = service.get_definition(field_id)
                if current and (current["label"], current["field_type"]) != (previous["label"],
                                                                            previous["field_type"]):
                    _audit_definition("profile_field.updated", current, previous_label=previous["label"],
                                      previous_field_type=previous["field_type"])
            elif action == "delete":
                values = service.value_count(field_id)
                service.delete_definition(field_id)
                _audit_definition("profile_field.deleted", previous, values=values)
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
