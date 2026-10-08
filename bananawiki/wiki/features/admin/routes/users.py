"""Account administration: list, create, manage, permissions, audit, impersonation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import abort, redirect, render_template, request, url_for

from .... import accounts, auth
from .... import permissions as perms
from ....accounts import AccountError
from ....features.pages import categories
from ....templating import from_local_input
from .. import roles as custom_roles
from .. import service
from ..blueprint import bp
from .common import actor, back, endpoint_url, page_number, refuse, target_or_404

# ── List and create ──────────────────────────────────────────────────────────


@bp.get("/admin/users")
@auth.admin_required
def users():
    service.expire_suspensions()
    flt = service.UserFilter.from_args(request.args)
    page = page_number()
    rows, total = service.list_users(flt, page)
    pages = max(1, -(-total // service.USERS_PER_PAGE))
    return render_template(
        "admin/users.html", users=rows, total=total, page=min(page, pages), pages=pages, flt=flt,
        custom_roles=custom_roles.list_roles(), pending=service.pending_count(),
    )


@bp.route("/admin/users/create", methods=["GET", "POST"])
@auth.admin_required
def create_user():
    form: dict[str, Any] = {}
    if request.method == "POST":
        form = request.form
        password = form.get("password", "")
        try:
            if password != form.get("confirm_password", ""):
                raise AccountError("auth.error.passwords_differ")
            user = service.create_user(form.get("username", ""), password, form.get("role", "user"),
                                       force_password_change=form.get("force_password_change") == "1")
        except AccountError as error:
            refuse(error)
        else:
            auth.flash_t("admin.users.created", "success", username=user["username"])
            return redirect(url_for("admin.user_detail", user_id=user["id"]))
    return render_template("admin/user_create.html", form=form)


# ── One account ──────────────────────────────────────────────────────────────


@bp.get("/admin/users/<string:user_id>")
@auth.admin_required
def user_detail(user_id: str):
    target = target_or_404(user_id)
    me = actor()
    return render_template(
        "admin/user_detail.html",
        target=target,
        protection=service.protection_error(me, target),
        impersonation=service.impersonation_error(me, target),
        is_self=me["id"] == target["id"],
        can_grant_owner=me["role"] == "owner" or service.is_superuser(me),
        actor_superuser=service.is_superuser(me),
        custom_role=custom_roles.get(target["custom_role_id"]) if target.get("custom_role_id") else None,
        custom_roles=custom_roles.list_roles(),
        sessions=service.active_sessions(target["id"]),
        current_session_id=auth.current_session_id(),
        contributions=service.contribution_count(target["id"]),
        profile_url=endpoint_url("users.profile", username=target["username"]),
        presets=service.SUSPENSION_PRESETS,
    )


def _suspend(me: dict[str, Any], target: dict[str, Any], form: Any) -> None:
    until, label = service.suspension_end(
        form.get("suspend_duration", "permanent"),
        from_local_input(form.get("suspend_custom_datetime")),
        form.get("suspend_rel_hours", ""),
        form.get("suspend_rel_minutes", ""),
    )
    service.suspend(me, target, until=until, label=label, reason=form.get("suspend_reason", ""),
                    reason_visible=form.get("suspend_reason_visible") == "1",
                    time_visible=form.get("suspend_time_visible") == "1")
    auth.flash_t("admin.suspend.done_until" if until else "admin.suspend.done", "success")


def _password(me: dict[str, Any], target: dict[str, Any], form: Any, *, keep_original: bool) -> None:
    password = form.get("password", "")
    if form.get("confirm_password") is not None and password != form.get("confirm_password"):
        raise AccountError("auth.error.passwords_differ")
    service.reset_password(me, target, password, require_change=form.get("force_password_change") == "1",
                           keep_original=keep_original or form.get("keep_original") == "1")
    auth.flash_t("admin.password.done", "success")


def _reset_password(me, target, form) -> None:
    _password(me, target, form, keep_original=False)


def _temporary_password(me, target, form) -> None:
    _password(me, target, form, keep_original=True)


def _rename(me: dict[str, Any], target: dict[str, Any], form: Any) -> None:
    service.rename(me, target, form.get("username", ""))
    auth.flash_t("admin.users.renamed", "success")


def _change_role(me: dict[str, Any], target: dict[str, Any], form: Any) -> None:
    service.change_role(me, target, form.get("role", ""))
    auth.flash_t("admin.users.role_changed", "success")


def _restore_password(me, target, form) -> None:
    service.restore_password(me, target)
    auth.flash_t("admin.password.restored", "success")


def _superuser(me, target, form) -> None:
    enabled = service.toggle_superuser(me, target)
    auth.flash_t("admin.users.superuser_on" if enabled else "admin.users.superuser_off", "success",
                 username=target["username"])


def _unsuspend(me, target, form) -> None:
    service.unsuspend(me, target)
    auth.flash_t("admin.suspend.lifted", "success")


def _approve(me, target, form) -> None:
    service.approve(me, target)
    auth.flash_t("admin.approval.approved", "success", username=target["username"])


def _deny(me, target, form) -> None:
    service.deny(me, target)
    auth.flash_t("admin.approval.denied", "success", username=target["username"])


ACTIONS: dict[str, Callable[[dict[str, Any], dict[str, Any], Any], None]] = {
    "change_username": _rename,
    "change_password": _reset_password,
    "set_temp_password": _temporary_password,
    "revert_password": _restore_password,
    "toggle_superuser": _superuser,
    "change_role": _change_role,
    "suspend": _suspend,
    "unsuspend": _unsuspend,
    "approve_user": _approve,
    "deny_user": _deny,
}


@bp.post("/admin/users/<string:user_id>/edit")
@auth.admin_required
def edit_user(user_id: str):
    """Every single-account action of the 1.4 user table, selected by ``action``."""
    target = target_or_404(user_id)
    me = actor()
    action = request.form.get("action", "")
    if action == "delete":
        return _delete(me, target)
    handler = ACTIONS.get(action)
    if handler is None:
        abort(400)
    try:
        handler(me, target, request.form)
    except AccountError as error:
        refuse(error)
    return back("admin.user_detail", user_id=target["id"])


def _delete(me: dict[str, Any], target: dict[str, Any]):
    try:
        service.delete_user(me, target)
    except AccountError as error:
        refuse(error)
        return back("admin.user_detail", user_id=target["id"])
    auth.flash_t("admin.users.deleted", "success", username=target["username"])
    return redirect(url_for("admin.users"))


@bp.post("/admin/users/<string:user_id>/assign-role")
@auth.admin_required
def assign_role(user_id: str):
    """Pick a standard role (``std:editor``) or a custom one (``custom:3``, ``custom:none``)."""
    target = target_or_404(user_id)
    me = actor()
    identity = request.form.get("identity", "").strip()
    kind, _, value = identity.partition(":")
    try:
        if kind == "std":
            service.change_role(me, target, value)
        elif kind == "custom" and value in ("", "none"):
            service.remove_custom_role(me, target)
        elif kind == "custom" and value.isdigit():
            role = custom_roles.get(int(value))
            if role is None:
                raise AccountError("admin.roles.error.not_found")
            service.assign_custom_role(me, target, role)
        else:
            raise AccountError("auth.error.invalid_role")
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.users.role_changed", "success")
    return back("admin.user_detail", user_id=target["id"])


@bp.post("/admin/users/<string:user_id>/toggle_chat")
@auth.admin_required
def toggle_chat(user_id: str):
    target = target_or_404(user_id)
    try:
        disabled = service.toggle_chat(actor(), target)
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.users.chat_off" if disabled else "admin.users.chat_on", "success",
                     username=target["username"])
    return back("admin.user_detail", user_id=target["id"])


@bp.post("/admin/users/<string:user_id>/sessions/revoke")
@auth.admin_required
def revoke_sessions(user_id: str):
    target = target_or_404(user_id)
    try:
        count = service.revoke_session(actor(), target, request.form.get("session_id") or None)
    except AccountError as error:
        refuse(error)
    else:
        auth.flash_t("admin.sessions.revoked", "success", count=count)
    return back("admin.user_detail", user_id=target["id"])


# ── Impersonation ────────────────────────────────────────────────────────────


@bp.post("/admin/users/<string:user_id>/impersonate")
@auth.admin_required
def impersonate(user_id: str):
    target = target_or_404(user_id)
    try:
        service.start_impersonation(actor(), target)
    except AccountError as error:
        refuse(error)
        return back("admin.user_detail", user_id=target["id"])
    auth.flash_t("admin.impersonate.started", "success", username=target["username"])
    return redirect("/")


@bp.post("/admin/stop-impersonating")
@auth.exempt("setup", "maintenance", "account_steps", "approval")
def stop_impersonating():
    """1.4 URL; the sign-in feature owns the action."""
    return redirect(url_for("auth.stop_impersonation"), code=307)


# ── Permissions ──────────────────────────────────────────────────────────────


def _category_ids(field: str) -> list[int]:
    return service.existing_category_ids(request.form.getlist(field))


@bp.route("/admin/users/<string:user_id>/permissions", methods=["GET", "POST"])
@auth.admin_required
def user_permissions(user_id: str):
    target = target_or_404(user_id)
    me = actor()
    if request.method == "POST":
        try:
            if request.form.get("action") == "reset":
                service.reset_overrides(me, target)
                auth.flash_t("admin.permissions.reset", "success")
            else:
                read_restricted = request.form.get("read_restricted") == "1"
                write_restricted = request.form.get("write_restricted") == "1"
                service.save_overrides(
                    me, target, keys=request.form.getlist("permissions"),
                    read_restricted=read_restricted,
                    read_ids=_category_ids("read_category_ids") if read_restricted else [],
                    write_restricted=write_restricted,
                    write_ids=_category_ids("write_category_ids") if write_restricted else [],
                )
                auth.flash_t("admin.permissions.saved", "success")
        except AccountError as error:
            refuse(error)
        return redirect(url_for("admin.user_permissions", user_id=target["id"]))
    overridable = target["role"] in ("user", "editor") and not target.get("custom_role_id")
    return render_template(
        "admin/user_permissions.html",
        target=target,
        protection=service.protection_error(me, target),
        overridable=overridable,
        access=service.user_access(target),
        groups=perms.grouped_for(target["role"]) if overridable else [],
        all_categories=categories.all_categories(),
    )


@bp.route("/admin/users/<string:user_id>/editor-access", methods=["GET", "POST"])
@auth.admin_required
def editor_access(user_id: str):
    target = target_or_404(user_id)
    me = actor()
    if request.method == "POST":
        restricted = request.form.get("restricted") == "1"
        try:
            service.save_editor_access(me, target, restricted=restricted,
                                       category_ids=_category_ids("category_ids") if restricted else [])
        except AccountError as error:
            refuse(error)
        else:
            auth.flash_t("admin.editor_access.saved", "success")
        return redirect(url_for("admin.editor_access", user_id=target["id"]))
    return render_template(
        "admin/editor_access.html",
        target=target,
        protection=service.protection_error(me, target),
        access=service.user_access(target),
        all_categories=categories.all_categories(),
    )


# ── Audit and attributions ───────────────────────────────────────────────────


@bp.get("/admin/users/<string:user_id>/audit")
@auth.admin_required
def user_audit(user_id: str):
    target = target_or_404(user_id)
    return render_template(
        "admin/user_audit.html",
        target=target,
        trail=service.audit_trail(target["id"]),
        reserved_names=accounts.reserved_names(target["id"]),
        contributions=service.contribution_count(target["id"]),
        protection=service.protection_error(actor(), target),
    )


@bp.post("/admin/users/<string:user_id>/attributions")
@auth.admin_required
def attributions(user_id: str):
    target = target_or_404(user_id)
    me = actor()
    action = request.form.get("action", "")
    try:
        if action == "deattribute_all":
            count = service.deattribute_all(me, target)
            auth.flash_t("admin.attributions.removed", "success", count=count)
        elif action == "mass_reattribute":
            recipient = accounts.by_username(request.form.get("to_username", "")) \
                or service.get_user(request.form.get("to_user_id", ""))
            if recipient is None:
                raise AccountError("admin.attributions.error.no_recipient")
            count = service.reattribute_all(me, target, recipient)
            auth.flash_t("admin.attributions.moved", "success", count=count, username=recipient["username"])
        elif action == "delete_role_history_entry":
            entry = request.form.get("entry_id", "")
            if not entry.isdigit():
                raise AccountError("admin.attributions.error.entry_not_found")
            service.delete_role_history(me, target, int(entry))
            auth.flash_t("admin.attributions.history_deleted", "success", count=1)
        elif action == "delete_all_role_history":
            count = service.delete_role_history(me, target)
            auth.flash_t("admin.attributions.history_deleted", "success", count=count)
        elif action == "release_name":
            name, holder = service.release_name(me, target, request.form.get("username", ""))
            if holder is None:
                auth.flash_t("admin.audit.name_released", "success", username=name)
            else:
                auth.flash_t("admin.audit.name_still_reserved", "warning", username=name,
                             holder=holder["username"])
        else:
            abort(400)
    except AccountError as error:
        refuse(error)
    return back("admin.user_audit", user_id=target["id"])
