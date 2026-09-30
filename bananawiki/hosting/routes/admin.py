"""Platform administration: accounts, wikis, settings, banners, merges, archives."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from ...core.timeutil import now_sql, sql_in
from .. import (
    accounts,
    attention,
    auth,
    banners,
    collaborators,
    domains,
    events,
    features,
    instances,
    invites,
    merges,
    notifications,
    oauth,
    settings,
    urls,
)
from ..db import db
from ..errors import ServiceError
from ..i18n import t
from ..limits import rate_limit
from ..runtime import RuntimeFailure
from .common import (
    account,
    admin_back,
    back,
    flash_error,
    int_field,
    seconds_from_form,
    suspension_from_form,
    utc_from_input,
)
from .dashboard import export_instance, stream_file

bp = Blueprint("admin", __name__)
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{16,96}$")


def _dashboard():
    return redirect(url_for("admin.dashboard"))


def _account_or_404(account_id: str) -> dict[str, Any]:
    target = accounts.get(account_id)
    if target is None:
        abort(404)
    return target


def _instance_or_404(instance_id: str) -> dict[str, Any]:
    inst = instances.get(instance_id)
    if inst is None:
        abort(404)
    return inst


def _done(key: str, **values: Any) -> None:
    flash(t(key, **values), "success")


# ── Overview ──────────────────────────────────────────────────────────────────


@bp.get("/admin")
@auth.admin_required
def dashboard():
    return render_template(
        "hosting/admin/dashboard.html", stats=instances.platform_stats(), pending=accounts.pending(),
        feature_queue=features.pending_queue(), signup_mode=settings.signup_mode(), invites=invites.list_all(),
        accounts=accounts.all_accounts(), instances=instances.admin_list(),
    )


@bp.get("/admin/attention", endpoint="attention")
@auth.admin_required
def attention_page():
    entries = attention.items(account(), with_oldest=True)
    return render_template("hosting/admin/attention.html", entries=entries,
                           total=sum(entry["count"] for entry in entries))


@bp.post("/admin/notifications/test")
@auth.admin_required
@rate_limit(5, 3600)
def send_test_email():
    ok, reason = notifications.send_test(account())
    if ok:
        _done("hosting.attention.test_sent", address=account()["email"])
    else:
        flash(t(f"hosting.attention.test_failed.{reason}", t("hosting.attention.test_failed.other")), "error")
    return redirect(url_for("admin.settings_page", open="notifications") + "#notifications")


@bp.get("/admin/moderation")
@auth.admin_required
def moderation():
    return render_template("hosting/admin/moderation.html",
                           events=events.recent(request.args.get("type"), request.args.get("id")))


# ── Accounts ──────────────────────────────────────────────────────────────────


@bp.get("/admin/accounts/<account_id>")
@auth.admin_required
def account_page(account_id: str):
    target = _account_or_404(account_id)
    return render_template("hosting/admin/account.html", target=target,
                           owned=db.all("SELECT * FROM instances WHERE account_id = ? ORDER BY created_at DESC",
                                        (account_id,)),
                           history=accounts.suspension_history(account_id),
                           events=events.recent("account", account_id, 50),
                           deletion_remaining=accounts.deletion_remaining(target))


@bp.post("/admin/accounts/create")
@auth.admin_required
@rate_limit(20)
def create_account():
    try:
        accounts.check_new_password(request.form.get("password") or "", request.form.get("confirm_password") or "")
        created = accounts.create(request.form.get("username") or "", request.form.get("password") or "",
                                  is_admin=bool(request.form.get("is_admin")), email=request.form.get("email") or "")
    except ServiceError as error:
        flash_error(error)
        return _dashboard()
    events.record("account", created["id"], "account.created_by_admin", account()["id"])
    _done("hosting.admin.account_created", username=created["username"])
    return _dashboard()


def _not_self(target: dict[str, Any]) -> None:
    if target["id"] == account()["id"]:
        raise ServiceError("hosting.admin.not_on_self")


@bp.post("/admin/accounts/<account_id>/toggle-admin")
@auth.admin_required
@rate_limit(20)
def toggle_admin(account_id: str):
    target = _account_or_404(account_id)
    try:
        _not_self(target)
        if target["deleted_at"] or target["approval_status"] != "approved":
            raise ServiceError("hosting.admin.not_approved")
        promote = not target["is_admin"]
        accounts.set_admin(account_id, promote)
        events.record("account", account_id, "account.admin." + ("granted" if promote else "revoked"), account()["id"])
        renamed = 0
        for inst in instances.owned_by(account_id):
            if not promote and (inst.get("domain_mode") or "hosting") == "apex":
                inst = instances.rename_for_non_admin(inst, actor_id=account()["id"])
                renamed += 1
            instances.apply_owner_quota(inst, promote)
            instances.apply_policy(inst, actor_id=account()["id"])
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.account_page", account_id=account_id))
    _done("hosting.admin.admin_granted" if promote else "hosting.admin.admin_revoked", username=target["username"])
    if renamed:
        flash(t("hosting.admin.apex_converted", count=renamed), "info")
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/impersonate")
@auth.admin_required
@rate_limit(10)
def impersonate(account_id: str):
    target = _account_or_404(account_id)
    if auth.impersonating():
        flash(t("hosting.admin.already_impersonating"), "error")
        return _dashboard()
    if (target["id"] == account()["id"] or target["is_admin"] or target["deleted_at"]
            or accounts.is_suspended(target) or target["approval_status"] != "approved"):
        flash(t("hosting.admin.cannot_impersonate"), "error")
        return _dashboard()
    auth.start_impersonation(target)
    events.record("account", target["id"], "account.impersonated", auth.real_account()["id"])  # type: ignore[index]
    _done("hosting.admin.impersonating", username=target["username"])
    return redirect(url_for("dashboard.dashboard"))


@bp.post("/admin/stop-impersonating")
@auth.exempt(*auth.GATES)
@rate_limit(10)
def stop_impersonating():
    if not auth.impersonating():
        flash(t("hosting.admin.not_impersonating"), "error")
        return redirect(url_for("dashboard.dashboard"))
    auth.stop_impersonation()
    _done("hosting.admin.impersonation_stopped")
    return _dashboard()


def _terminate_or_transfer(target: dict[str, Any], transfer_to: str) -> None:
    owned = instances.owned_by(target["id"])
    if transfer_to:
        recipient = accounts.active_by_username(transfer_to)
        if recipient is None or recipient["id"] == target["id"]:
            raise ServiceError("hosting.accounts.not_found")
        for inst in owned:
            instances.move_to_owner(inst, recipient, actor_id=account()["id"])
        return
    for inst in owned:
        instances.terminate(inst, actor_id=account()["id"], reason="account_deleted")


@bp.route("/admin/accounts/<account_id>/delete", methods=["GET", "POST"])
@auth.admin_required
@rate_limit(10)
def delete_account(account_id: str):
    target = _account_or_404(account_id)
    if request.method == "GET":
        return render_template("hosting/admin/delete_account.html", target=target, owned=instances.owned_by(account_id))
    try:
        _not_self(target)
        transfer_to = (request.form.get("transfer_username") or "").strip() \
            if request.form.get("instance_action") == "transfer" else ""
        _terminate_or_transfer(target, transfer_to)
        address, username = target["email"], target["username"]
        accounts.delete(account_id)
        events.record("account", account_id, "account.deleted", account()["id"])
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("admin.delete_account", account_id=account_id))
    if address and request.form.get("notify_user"):
        notifications.send(address, "account_deleted", username=username)
    _done("hosting.admin.account_deleted", username=username)
    return _dashboard()


@bp.post("/admin/accounts/bulk-delete")
@auth.admin_required
@rate_limit(5)
def bulk_delete_accounts():
    done = 0
    for account_id in request.form.getlist("account_ids")[:200]:
        target = accounts.get(account_id)
        if target is None or target["id"] == account()["id"] or target["deleted_at"]:
            continue
        try:
            _terminate_or_transfer(target, "")
            accounts.delete(account_id)
            events.record("account", account_id, "account.deleted", account()["id"])
            done += 1
        except ServiceError:
            continue
    _done("hosting.admin.accounts_deleted", count=done)
    return _dashboard()


@bp.post("/admin/accounts/<account_id>/schedule-deletion")
@auth.admin_required
@rate_limit(10)
def schedule_deletion(account_id: str):
    target = _account_or_404(account_id)
    try:
        _not_self(target)
        seconds = seconds_from_form("deletion") or 86400
        reason = (request.form.get("deletion_reason") or "").strip()
        accounts.schedule_deletion(account_id, seconds, reason, account()["id"])
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.account_page", account_id=account_id))
    if request.form.get("notify_user"):
        notifications.notify_account(target, "deletion_scheduled", reason=reason, hours=round(seconds / 3600, 1))
    _done("hosting.admin.deletion_scheduled", username=target["username"])
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/cancel-deletion")
@auth.admin_required
@rate_limit(10)
def cancel_deletion(account_id: str):
    target = _account_or_404(account_id)
    accounts.cancel_deletion(account_id, account()["id"])
    _done("hosting.admin.deletion_cancelled", username=target["username"])
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/suspend")
@auth.admin_required
@rate_limit(20)
def suspend_account(account_id: str):
    target = _account_or_404(account_id)
    try:
        _not_self(target)
        if target["is_admin"] and accounts.count_active_admins(exclude=account_id) == 0:
            raise ServiceError("hosting.admin.last_admin")
        until, label = suspension_from_form()
        reason = (request.form.get("suspend_reason") or "").strip()
        reason_visible = request.form.get("suspend_reason_visible") == "1"
        time_visible = request.form.get("suspend_time_visible") == "1"
        accounts.suspend(account_id, until=until, reason=reason, reason_visible=reason_visible,
                         time_visible=time_visible, actor_id=account()["id"], duration_label=label)
        if request.form.get("suspend_instances") == "1":
            for inst in instances.owned_by(account_id):
                if inst["status"] in ("running", "stopped"):
                    instances.suspend(inst, actor_id=account()["id"], reason=reason, duration_label="account")
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.account_page", account_id=account_id))
    if request.form.get("notify_user", "1") != "0":
        notifications.notify_account(target, "account_suspended", reason=reason if reason_visible else "",
                                     until=until if time_visible and until else "")
    _done("hosting.admin.account_suspended", username=target["username"])
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/unsuspend")
@auth.admin_required
@rate_limit(20)
def unsuspend_account(account_id: str):
    target = _account_or_404(account_id)
    accounts.unsuspend(account_id, actor_id=account()["id"])
    restored = 0
    if request.form.get("unsuspend_instances") == "1":
        for inst in instances.owned_by(account_id):
            if inst["status"] == "suspended":
                try:
                    instances.unsuspend(inst, actor_id=account()["id"])
                    restored += 1
                except ServiceError:
                    continue
    if request.form.get("notify_user", "1") != "0":
        notifications.notify_account(target, "account_restored")
    _done("hosting.admin.account_unsuspended", username=target["username"], count=restored)
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/edit")
@auth.admin_required
@rate_limit(30)
def edit_account(account_id: str):
    target = _account_or_404(account_id)
    username = (request.form.get("new_username") or "").strip() or target["username"]
    email = (request.form.get("new_email") or "").strip().lower()
    password = request.form.get("new_password") or ""
    try:
        if target["deleted_at"]:
            raise ServiceError("hosting.accounts.not_found")
        email_changed = False
        if username != target["username"] or (email and email != (target["email"] or "")):
            email_changed = accounts.update_identity(target, username=username, email=email or target["email"],
                                                     email_required=False)
        if password:
            accounts.check_new_password(password, request.form.get("confirm_password") or "")
            accounts.set_password(account_id, password, actor_id=account()["id"], reason="Password set by an administrator")
        events.record("account", account_id, "account.edited", account()["id"])
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.account_page", account_id=account_id))
    if email_changed and notifications.verification_required():
        notifications.send_verification(accounts.get(account_id), ignore_cooldown=True)  # type: ignore[arg-type]
    _done("hosting.admin.account_edited", username=username)
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/flag-email")
@auth.admin_required
@rate_limit(20)
def flag_email(account_id: str):
    target = _account_or_404(account_id)
    try:
        accounts.flag_email(target, reason=(request.form.get("reason") or "").strip(),
                            reason_visible=request.form.get("reason_visible") == "1",
                            replacement=(request.form.get("new_email") or "").strip(), actor_id=account()["id"])
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.account_page", account_id=account_id))
    _done("hosting.admin.email_flagged", username=target["username"])
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/unflag-email")
@auth.admin_required
@rate_limit(20)
def unflag_email(account_id: str):
    target = _account_or_404(account_id)
    accounts.clear_email_flag(account_id)
    _done("hosting.admin.email_unflagged", username=target["username"])
    return back(url_for("admin.account_page", account_id=account_id))


@bp.post("/admin/accounts/<account_id>/approve")
@auth.admin_required
@rate_limit(20)
def approve_account(account_id: str):
    target = _account_or_404(account_id)
    reason = request.form.get("decision_reason") or ""
    try:
        if not accounts.approve(account_id, account()["id"], reason):
            raise ServiceError("hosting.admin.not_awaiting")
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.dashboard"))
    notifications.notify_account(target, "account_approved", reason=reason.strip())
    _done("hosting.admin.approved", username=target["username"])
    return back(url_for("admin.dashboard"))


@bp.post("/admin/accounts/<account_id>/deny")
@auth.admin_required
@rate_limit(20)
def deny_account(account_id: str):
    target = _account_or_404(account_id)
    reason = request.form.get("decision_reason") or ""
    try:
        if not accounts.deny(account_id, account()["id"], reason):
            raise ServiceError("hosting.admin.not_awaiting")
    except ServiceError as error:
        flash_error(error)
        return back(url_for("admin.dashboard"))
    notifications.notify_account(target, "account_denied", reason=reason.strip())
    _done("hosting.admin.denied", username=target["username"])
    return back(url_for("admin.dashboard"))


# ── Sign-up gating ────────────────────────────────────────────────────────────


@bp.post("/admin/signup-mode")
@auth.admin_required
@rate_limit(20)
def signup_mode():
    mode = (request.form.get("signup_mode") or "").strip().lower()
    if mode not in settings.SIGNUP_MODES:
        flash(t("hosting.admin.invalid_signup_mode"), "error")
        return _dashboard()
    was_approval = settings.approval_required()
    settings.update(signup_mode=mode, hosting_activation_required=1 if mode == "approval" else 0)
    if was_approval and mode != "approval":
        count = accounts.approve_all_pending(account()["id"])
        if count:
            flash(t("hosting.admin.auto_approved", count=count), "info")
    _done("hosting.admin.signup_mode_saved", mode=t(f"hosting.signup_mode.{mode}"))
    return _dashboard()


@bp.post("/admin/invites/create")
@auth.admin_required
@rate_limit(20)
def create_invite():
    try:
        expires = utc_from_input((request.form.get("expires_at") or "").strip())
        row = invites.create(account()["id"], note=request.form.get("note") or "",
                             max_uses=int_field("max_uses", 1, minimum=1, maximum=1000),
                             expires_at=expires, code=(request.form.get("custom_code") or "").strip() or None)
    except ServiceError as error:
        flash_error(error)
        return _dashboard()
    _done("hosting.admin.invite_created", code=row["code"])
    return _dashboard()


@bp.post("/admin/invites/<int:invite_id>/delete")
@auth.admin_required
@rate_limit(20)
def delete_invite(invite_id: int):
    if invites.delete(invite_id):
        _done("hosting.admin.invite_deleted")
    else:
        flash(t("hosting.invites.invalid"), "error")
    return _dashboard()


# ── Wikis ─────────────────────────────────────────────────────────────────────


@bp.get("/admin/instances/<instance_id>/manage")
@bp.get("/admin/instances/<instance_id>")
@auth.admin_required
def instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    runtime_spec = instances.spec(inst, with_policy=False)
    users, total, quarantined, snapshots = [], 0, False, []
    try:
        if inst["status"] != "terminated":
            users, total = instances.runtime().list_users(runtime_spec, limit=200)
        quarantined = instances.runtime().plugins_quarantined(runtime_spec)
        snapshots = instances.runtime().list_plugin_snapshots(runtime_spec)
    except (RuntimeFailure, ValueError):
        pass
    return render_template(
        "hosting/admin/instance.html", instance=instances.describe(inst), owner=accounts.get(inst["account_id"]),
        users=users, users_total=total, quarantined=quarantined, snapshots=snapshots,
        history=instances.suspension_history(instance_id), upload_policy=instances.upload_policy(inst),
        feature_history=features.history(instance_id), domain=domains.binding(instance_id),
        events=events.recent("instance", instance_id, 50), collaborators=collaborators.list_for(instance_id),
    )


def _instance_action(instance_id: str, action, success_key: str):
    inst = _instance_or_404(instance_id)
    try:
        action(inst)
        _done(success_key, slug=urls.original_slug(inst["subdomain"]) or inst["subdomain"])
    except ServiceError as error:
        flash_error(error)
    return admin_back(instance_id)


def _actor() -> str:
    return account()["id"]


@bp.post("/admin/instances/<instance_id>/stop")
@auth.admin_required
@rate_limit(20)
def stop_instance(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.stop(i, actor_id=_actor(), allow_suspended=True),
                            "hosting.instances.stopped")


@bp.post("/admin/instances/<instance_id>/restart")
@auth.admin_required
@rate_limit(20)
def restart_instance(instance_id: str):
    def act(inst):
        if inst["status"] == "running":
            instances.restart(inst, actor_id=_actor())
        else:
            instances.start(inst, actor_id=_actor(), allow_suspended=True)

    return _instance_action(instance_id, act, "hosting.instances.started")


@bp.post("/admin/instances/<instance_id>/force-restart")
@auth.admin_required
@rate_limit(10)
def force_restart(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.restart(i, actor_id=_actor()), "hosting.instances.started")


@bp.post("/admin/instances/<instance_id>/extend")
@auth.admin_required
@rate_limit(20)
def extend(instance_id: str):
    def act(inst):
        seconds = seconds_from_form("extra")
        if seconds < 1:
            raise ServiceError("hosting.form.invalid_number")
        instances.shift_expiry(inst, seconds, actor_id=_actor())

    return _instance_action(instance_id, act, "hosting.admin.expiry_changed")


@bp.post("/admin/instances/<instance_id>/shorten-expiry")
@auth.admin_required
@rate_limit(20)
def shorten(instance_id: str):
    def act(inst):
        seconds = seconds_from_form("shorten")
        if seconds < 1:
            raise ServiceError("hosting.form.invalid_number")
        instances.shift_expiry(inst, -seconds, actor_id=_actor())

    return _instance_action(instance_id, act, "hosting.admin.expiry_changed")


@bp.post("/admin/instances/<instance_id>/set-expiry")
@auth.admin_required
@rate_limit(20)
def set_expiry(instance_id: str):
    def act(inst):
        value = utc_from_input((request.form.get("expiry_datetime") or "").strip())
        if not value:
            raise ServiceError("hosting.form.invalid_datetime")
        instances.set_expiry(inst, value, actor_id=_actor())

    return _instance_action(instance_id, act, "hosting.admin.expiry_changed")


@bp.post("/admin/instances/<instance_id>/make-indefinite")
@auth.admin_required
@rate_limit(20)
def make_indefinite(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.set_expiry(
        i, None if (i.get("domain_mode") or "hosting") == "apex" else sql_in(days=instances.ADMIN_EXPIRY_DAYS),
        actor_id=_actor()), "hosting.admin.expiry_changed")


@bp.post("/admin/instances/<instance_id>/terminate")
@auth.admin_required
@rate_limit(20)
def terminate_instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        instances.terminate(inst, actor_id=_actor(), reason="admin")
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    if request.form.get("notify_user"):
        notifications.notify_account(accounts.get(inst["account_id"]), "instance_terminated", slug=inst["subdomain"])
    _done("hosting.instances.terminated_done", slug=inst["subdomain"])
    return admin_back(instance_id)


@bp.post("/admin/instances/<instance_id>/restore")
@auth.admin_required
@rate_limit(10)
def restore_instance(instance_id: str):
    def act(inst):
        days = int_field("extend_days", 0, minimum=0, maximum=36500)
        instances.restore(inst, actor_id=_actor(), extend_days=days or None)

    return _instance_action(instance_id, act, "hosting.admin.restored")


@bp.post("/admin/instances/<instance_id>/delete-terminated")
@auth.admin_required
@rate_limit(10)
def delete_terminated(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        instances.hard_delete(inst, actor_id=_actor())
        _done("hosting.admin.instance_deleted")
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    return _dashboard()


@bp.post("/admin/instances/<instance_id>/grace-suspend")
@auth.admin_required
@rate_limit(20)
def grace_suspend(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.set_grace_suspended(i, True, actor_id=_actor()),
                            "hosting.admin.grace_suspended")


@bp.post("/admin/instances/<instance_id>/grace-unsuspend")
@auth.admin_required
@rate_limit(20)
def grace_unsuspend(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.set_grace_suspended(i, False, actor_id=_actor()),
                            "hosting.admin.grace_unsuspended")


@bp.post("/admin/instances/<instance_id>/suspend")
@auth.admin_required
@rate_limit(20)
def suspend_instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        until, label = suspension_from_form()
        reason = (request.form.get("suspend_reason") or "").strip()
        instances.suspend(inst, actor_id=_actor(), until=until, reason=reason,
                          reason_visible=request.form.get("suspend_reason_visible") == "1",
                          time_visible=request.form.get("suspend_time_visible") == "1", duration_label=label)
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    if request.form.get("notify_user"):
        notifications.notify_account(accounts.get(inst["account_id"]), "instance_suspended", slug=inst["subdomain"],
                                     reason=reason if request.form.get("suspend_reason_visible") == "1" else "")
    _done("hosting.admin.instance_suspended", slug=inst["subdomain"])
    return admin_back(instance_id)


@bp.post("/admin/instances/<instance_id>/unsuspend")
@auth.admin_required
@rate_limit(20)
def unsuspend_instance(instance_id: str):
    return _instance_action(instance_id, lambda i: instances.unsuspend(i, actor_id=_actor()),
                            "hosting.admin.instance_unsuspended")


@bp.post("/admin/instances/<instance_id>/transfer")
@auth.admin_required
@rate_limit(20)
def transfer_instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    target = accounts.active_by_username((request.form.get("username") or "").strip())
    try:
        if target is None or target["approval_status"] != "approved":
            raise ServiceError("hosting.accounts.not_found")
        if target["id"] == inst["account_id"]:
            raise ServiceError("hosting.transfers.already_owner")
        notes = instances.move_to_owner(inst, target, actor_id=_actor())
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    _done("hosting.admin.transferred", username=target["username"])
    if "renamed" in notes:
        flash(t("hosting.admin.apex_converted", count=1), "info")
    return admin_back(instance_id)


@bp.post("/admin/instances/<instance_id>/rename")
@auth.admin_required
@rate_limit(20)
def rename_instance(instance_id: str):
    mode = request.form.get("domain_mode", "hosting")
    return _instance_action(instance_id, lambda i: instances.rename(
        i, request.form.get("subdomain") or "", mode if mode in urls.DOMAIN_MODES else "hosting", actor_id=_actor()),
        "hosting.admin.renamed")


@bp.post("/admin/instances/<instance_id>/duplicate")
@auth.admin_required
@rate_limit(5)
def duplicate_instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    owner_name = (request.form.get("owner_username") or "").strip()
    if owner_name == "__self__":
        owner = account()
    elif owner_name:
        owner = accounts.active_by_username(owner_name)
    else:
        owner = accounts.get(inst["account_id"])
    try:
        if owner is None:
            raise ServiceError("hosting.accounts.not_found")
        mode = request.form.get("domain_mode", "hosting")
        copy = instances.duplicate(inst, owner, request.form.get("subdomain") or "",
                                   mode if mode in urls.DOMAIN_MODES else "hosting", actor_id=_actor())
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    _done("hosting.admin.duplicated", slug=copy["subdomain"])
    return redirect(url_for("admin.instance", instance_id=copy["id"]))


def _credentials(inst: dict[str, Any], username: str, password: str, kind: str):
    response = make_response(render_template("hosting/dashboard/credentials.html", instance=inst, username=username,
                                             password=password, kind=kind, url=urls.instance_url(inst)))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/admin/instances/<instance_id>/reset-password")
@auth.admin_required
@rate_limit(10)
def reset_instance_password(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        username, password = instances.reset_admin_password(inst, actor_id=_actor())
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    return _credentials(inst, username, password, "password_reset")


@bp.post("/admin/instances/<instance_id>/reset-wiki")
@auth.admin_required
@rate_limit(5, 300)
def reset_wiki(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        username, password = instances.reset_content(inst, actor_id=_actor())
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)
    return _credentials(inst, username, password, "reset")


@bp.post("/admin/instances/<instance_id>/storage-limit")
@auth.admin_required
@rate_limit(20)
def storage_limit(instance_id: str):
    def act(inst):
        limit = 0 if request.form.get("unlimited") == "1" else int_field("storage_limit_mb", minimum=1)
        if not instances.set_storage_limit(inst, limit, actor_id=_actor()):
            raise ServiceError("hosting.instances.restart_failed")

    return _instance_action(instance_id, act, "hosting.admin.limits_saved")


@bp.post("/admin/instances/<instance_id>/upload-policy")
@auth.admin_required
@rate_limit(10)
def upload_policy(instance_id: str):
    def act(inst):
        inherit = request.form.get("inherit_upload_size") == "1" or not (request.form.get("upload_max_size_mb") or "").strip()
        size = None if inherit else int_field("upload_max_size_mb", minimum=1, maximum=2048)
        if not instances.set_upload_policy(inst, size, (request.form.get("upload_blocked_extensions") or "")[:2000],
                                           actor_id=_actor()):
            raise ServiceError("hosting.instances.restart_failed")

    return _instance_action(instance_id, act, "hosting.admin.limits_saved")


def _runtime_call(instance_id: str, call, success_key: str, event: str | None = None):
    inst = _instance_or_404(instance_id)
    try:
        result = call(instances.runtime(), instances.spec(inst))
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        if event:
            events.record("instance", instance_id, event + ".failed", _actor(), error.code)
        return admin_back(instance_id)
    if event:
        events.record("instance", instance_id, event, _actor(), str(result or "")[:500])
    _done(success_key)
    return admin_back(instance_id)


@bp.post("/admin/instances/<instance_id>/users/set-password")
@auth.admin_required
@rate_limit(20)
def set_user_password(instance_id: str):
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role", "user")
    if not username or len(password) < 8 or role not in ("user", "editor", "admin", "owner"):
        flash(t("hosting.admin.invalid_wiki_user"), "error")
        return admin_back(instance_id)
    return _runtime_call(instance_id, lambda r, s: r.set_user_password(s, username, password, role),
                         "hosting.admin.wiki_user_saved", "wiki.user_password_set")


@bp.post("/admin/instances/<instance_id>/users/remove")
@auth.admin_required
@rate_limit(20)
def remove_user(instance_id: str):
    username = (request.form.get("username") or "").strip()
    return _runtime_call(instance_id, lambda r, s: r.remove_user(s, username), "hosting.admin.wiki_user_removed",
                         "wiki.user_removed")


@bp.post("/admin/instances/<instance_id>/quarantine-plugins")
@auth.admin_required
@rate_limit(3, 300)
def quarantine_plugins(instance_id: str):
    return _runtime_call(instance_id, lambda r, s: r.quarantine_plugins(s), "hosting.admin.plugins_quarantined",
                         "plugins.quarantined")


@bp.post("/admin/instances/<instance_id>/lift-plugin-quarantine")
@auth.admin_required
@rate_limit(3, 300)
def lift_quarantine(instance_id: str):
    return _runtime_call(instance_id, lambda r, s: r.lift_plugin_quarantine(s), "hosting.admin.quarantine_lifted",
                         "plugins.quarantine_lifted")


@bp.post("/admin/instances/<instance_id>/capture-plugin-snapshot")
@auth.admin_required
@rate_limit(5, 300)
def capture_snapshot(instance_id: str):
    return _runtime_call(instance_id, lambda r, s: r.capture_plugin_snapshot(s), "hosting.admin.snapshot_saved",
                         "plugins.snapshot_saved")


@bp.post("/admin/instances/<instance_id>/restore-plugin-snapshot")
@auth.admin_required
@rate_limit(2, 300)
def restore_snapshot(instance_id: str):
    name = (request.form.get("snapshot") or "").strip() or None
    return _runtime_call(instance_id, lambda r, s: r.restore_plugin_snapshot(s, name), "hosting.admin.snapshot_restored",
                         "plugins.snapshot_restored")


@bp.get("/admin/instances/<instance_id>/logs")
@auth.admin_required
@rate_limit(30)
def instance_logs(instance_id: str):
    inst = _instance_or_404(instance_id)
    name = request.args.get("log", "error.log")
    if name not in instances.LOG_NAMES:
        abort(400)
    try:
        tail = instances.runtime().logs(instances.spec(inst, with_policy=False), name)
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return admin_back(instance_id)
    return render_template("hosting/admin/logs.html", instance=inst, name=name, tail=tail)


@bp.get("/admin/instances/<instance_id>/download")
@auth.admin_required
@rate_limit(10)
def download_instance(instance_id: str):
    inst = _instance_or_404(instance_id)
    try:
        return export_instance(inst)
    except ServiceError as error:
        flash_error(error)
        return admin_back(instance_id)


@bp.post("/admin/instances/<instance_id>/features/<feature>/entitlement")
@auth.admin_required
@rate_limit(10)
def set_entitlement(instance_id: str, feature: str):
    if feature not in features.FEATURES or request.form.get("allowed") not in ("0", "1"):
        abort(400)
    allowed = request.form.get("allowed") == "1"

    def act(inst):
        if not features.set_entitlement(inst, feature, allowed, account()):
            raise ServiceError("hosting.instances.restart_failed")

    return _instance_action(instance_id, act, "hosting.admin.entitlement_saved")


@bp.post("/admin/instances/<instance_id>/toggle-public-wiki")
@auth.admin_required
@rate_limit(10)
def toggle_public(instance_id: str):
    def act(inst):
        if not features.set_entitlement(inst, "public_access", not inst.get("public_wiki_allowed"), account()):
            raise ServiceError("hosting.instances.restart_failed")

    return _instance_action(instance_id, act, "hosting.admin.entitlement_saved")


@bp.post("/admin/instances/<instance_id>/domain-permission")
@auth.admin_required
@rate_limit(20)
def domain_permission(instance_id: str):
    inst = _instance_or_404(instance_id)
    domains.set_permission(inst, request.form.get("allowed") == "1", _actor())
    instances.sync_routes()
    _done("hosting.admin.domain_permission_saved")
    return back(url_for("dashboard.instance_domain", instance_id=instance_id))


@bp.post("/admin/instances/<instance_id>/rotate-oauth-credentials")
@auth.admin_required
@rate_limit(10)
def rotate_oauth(instance_id: str):
    inst = _instance_or_404(instance_id)
    oauth.rotate_credentials(instance_id)
    events.record("instance", instance_id, "oauth.rotated", _actor())
    instances.apply_policy(inst, actor_id=_actor())
    _done("hosting.admin.oauth_rotated")
    return admin_back(instance_id)


def _admin_bulk(action, allowed: tuple[str, ...], key: str):
    done = skipped = 0
    for instance_id in request.form.getlist("instance_ids")[:500]:
        inst = instances.get(instance_id)
        if inst is None:
            continue
        if inst["status"] not in allowed:
            skipped += 1
            continue
        try:
            action(inst)
            done += 1
        except ServiceError:
            skipped += 1
    flash(t(key, done=done, skipped=skipped), "success")
    return _dashboard()


@bp.post("/admin/instances/bulk-stop")
@auth.admin_required
@rate_limit(10)
def bulk_stop():
    return _admin_bulk(lambda i: instances.stop(i, actor_id=_actor()), ("running",), "hosting.bulk.stopped")


@bp.post("/admin/instances/bulk-restart")
@auth.admin_required
@rate_limit(10)
def bulk_restart():
    return _admin_bulk(lambda i: instances.start(i, actor_id=_actor()), ("stopped",), "hosting.bulk.started")


@bp.post("/admin/instances/bulk-suspend")
@auth.admin_required
@rate_limit(10)
def bulk_suspend():
    return _admin_bulk(lambda i: instances.suspend(i, actor_id=_actor()), ("running", "stopped"),
                       "hosting.bulk.suspended")


@bp.post("/admin/instances/bulk-unsuspend")
@auth.admin_required
@rate_limit(10)
def bulk_unsuspend():
    return _admin_bulk(lambda i: instances.unsuspend(i, actor_id=_actor()), ("suspended",), "hosting.bulk.unsuspended")


@bp.post("/admin/instances/bulk-delete")
@auth.admin_required
@rate_limit(5)
def bulk_terminate():
    return _admin_bulk(lambda i: instances.terminate(i, actor_id=_actor(), reason="admin"),
                       ("running", "stopped", "suspended"), "hosting.bulk.terminated")


@bp.post("/admin/instances/bulk-restore-terminated")
@auth.admin_required
@rate_limit(5, 300)
def bulk_restore():
    return _admin_bulk(lambda i: instances.restore(i, actor_id=_actor()), ("terminated",), "hosting.bulk.restored")


@bp.post("/admin/instances/bulk-delete-terminated")
@auth.admin_required
@rate_limit(5, 300)
def bulk_purge():
    return _admin_bulk(lambda i: instances.hard_delete(i, actor_id=_actor()), ("terminated",), "hosting.bulk.deleted")


# ── Feature requests ──────────────────────────────────────────────────────────


def _review(request_id: int, decision: str):
    try:
        row = features.review(request_id, account(), decision, request.form.get("review_note") or "")
    except ServiceError as error:
        flash_error(error)
        return _dashboard()
    _done(f"hosting.features.review_{decision}", feature=t(f"hosting.features.{row['feature']}"))
    return back(url_for("admin.instance", instance_id=row["instance_id"]))


@bp.post("/admin/feature-requests/<int:request_id>/approve")
@auth.admin_required
@rate_limit(20)
def approve_feature(request_id: int):
    return _review(request_id, "approved")


@bp.post("/admin/feature-requests/<int:request_id>/deny")
@auth.admin_required
@rate_limit(20)
def deny_feature(request_id: int):
    return _review(request_id, "denied")


# ── Banners ───────────────────────────────────────────────────────────────────


def _banner_form() -> tuple[dict[str, Any], list[str]]:
    custom = bool(request.form.get("custom_colors"))
    values = {
        "content": request.form.get("content"), "color": request.form.get("color", "orange"),
        "visibility": request.form.get("visibility", "both"), "audience_mode": request.form.get("audience_mode", "all"),
        "expires_at": utc_from_input((request.form.get("expires_at") or "").strip()),
        "not_removable": request.form.get("not_removable"), "show_countdown": request.form.get("show_countdown"),
        "custom_background": request.form.get("custom_background") if custom else None,
        "custom_text_color": request.form.get("custom_text_color") if custom else None,
    }
    return values, request.form.getlist("audience_account_ids")


@bp.get("/admin/banners")
@auth.admin_required
def banners_page():
    active_accounts = [a for a in accounts.all_accounts() if not a["deleted_at"]]
    return render_template("hosting/admin/banners.html", all_banners=banners.list_all(), accounts=active_accounts,
                           colors=banners.COLORS, visibilities=banners.VISIBILITIES, audiences=banners.AUDIENCES)


@bp.post("/admin/banners/create")
@auth.admin_required
@rate_limit(10)
def create_banner():
    try:
        values, ids = _banner_form()
        banners.create(values, ids, account()["id"])
        _done("hosting.banners.created")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("admin.banners_page"))


@bp.post("/admin/banners/<int:banner_id>/edit")
@auth.admin_required
@rate_limit(10)
def edit_banner(banner_id: int):
    if banners.get(banner_id) is None:
        abort(404)
    try:
        values, ids = _banner_form()
        banners.update(banner_id, values, ids, bool(request.form.get("is_active")))
        _done("hosting.banners.updated")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("admin.banners_page"))


@bp.post("/admin/banners/<int:banner_id>/delete")
@auth.admin_required
@rate_limit(10)
def delete_banner(banner_id: int):
    if not banners.delete(banner_id):
        abort(404)
    _done("hosting.banners.deleted")
    return redirect(url_for("admin.banners_page"))


# ── Merges ────────────────────────────────────────────────────────────────────


@bp.route("/admin/merge-accounts", methods=["GET", "POST"])
@auth.admin_required
@rate_limit(10)
def merge_accounts():
    if request.method == "POST":
        try:
            moved = merges.admin_merge(request.form.get("source_username") or "", request.form.get("target_username") or "",
                                       account())
        except ServiceError as error:
            flash_error(error)
            return render_template("hosting/admin/merge_accounts.html"), 400
        _done("hosting.merges.completed", count=moved)
        return redirect(url_for("admin.merge_requests"))
    return render_template("hosting/admin/merge_accounts.html")


@bp.get("/admin/merge-requests")
@auth.admin_required
def merge_requests():
    return render_template("hosting/admin/merge_requests.html", merges=merges.recent())


def _merge_action(merge_id: int, action, key: str):
    try:
        result = action()
        _done(key, count=result if isinstance(result, int) else 0)
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("admin.merge_requests"))


@bp.post("/admin/merge-requests/<int:merge_id>/approve")
@auth.admin_required
@rate_limit(20)
def approve_merge(merge_id: int):
    return _merge_action(merge_id, lambda: merges.approve(merge_id, account(), as_admin=True), "hosting.merges.approved")


@bp.post("/admin/merge-requests/<int:merge_id>/deny")
@auth.admin_required
@rate_limit(20)
def deny_merge(merge_id: int):
    return _merge_action(merge_id, lambda: merges.close(merge_id, account(), "denied"), "hosting.merges.denied")


@bp.post("/admin/merge-requests/<int:merge_id>/cancel")
@auth.admin_required
@rate_limit(20)
def cancel_merge(merge_id: int):
    return _merge_action(merge_id, lambda: merges.close(merge_id, account(), "cancelled"), "hosting.merges.cancelled")


@bp.post("/admin/merge-requests/<int:merge_id>/execute")
@auth.admin_required
@rate_limit(10)
def execute_merge(merge_id: int):
    return _merge_action(merge_id, lambda: merges.execute(merge_id, account()), "hosting.merges.completed")


# ── Settings ──────────────────────────────────────────────────────────────────


def _checked(name: str) -> int:
    return 1 if request.form.get(name) else 0


def _apply_to_all_running() -> int:
    """Restart running wikis so a changed policy reaches them; returns failures."""
    failures = 0
    for inst in db.all("SELECT * FROM instances WHERE status IN ('running', 'stopped')"):
        if not instances.apply_policy(inst, actor_id=account()["id"]):
            failures += 1
    return failures


def _save_settings(action: str) -> str:
    """Apply one settings form; returns the success translation key."""
    if action == "save_limits":
        settings.update(global_limit_enabled=_checked("global_limit_enabled"),
                        global_limit_max_instances=int_field("global_limit_max_instances", 50, minimum=1),
                        global_limit_max_storage_mb=int_field("global_limit_max_storage_mb", 5000, minimum=1))
    elif action == "save_bot_protection":
        settings.update(bot_protection_enabled=_checked("bot_protection_enabled"))
    elif action == "save_account_communications":
        before = (settings.flag("forbid_non_admin_public_wikis", True), settings.flag("forbid_non_admin_page_builder", True))
        settings.update(
            ask_email_new_signup=_checked("ask_email_new_signup"), ask_email_existing_users=_checked("ask_email_existing_users"),
            email_required=_checked("email_required"), email_verification_required=_checked("email_verification_required"),
            email_verification_cooldown_seconds=int_field("email_verification_cooldown_seconds", 60, minimum=30, maximum=3600),
            forbid_non_admin_public_wikis=_checked("forbid_non_admin_public_wikis"),
            forbid_non_admin_page_builder=_checked("forbid_non_admin_page_builder"),
            auto_approve_public_access_requests=_checked("auto_approve_public_access_requests"),
            auto_approve_page_builder_requests=_checked("auto_approve_page_builder_requests"),
        )
        after = (settings.flag("forbid_non_admin_public_wikis", True), settings.flag("forbid_non_admin_page_builder", True))
        if before != after and _apply_to_all_running():
            flash(t("hosting.admin.some_restarts_failed"), "error")
    elif action == "save_notifications":
        notify = (request.form.get("approval_notify_email") or "").strip()
        if notify and (len(notify) > 254 or not accounts.EMAIL.match(notify)):
            raise ServiceError("hosting.accounts.invalid_email")
        mode = request.form.get("approval_notify_mode", "digest")
        if mode not in settings.APPROVAL_NOTIFY_MODES:
            raise ServiceError("hosting.form.invalid_choice")
        low, high = settings.NOTIFY_INTERVAL_MINUTES
        settings.update(
            approval_notify_email=notify, approval_notify_mode=mode,
            approval_notify_admins=_checked("approval_notify_admins"),
            approval_notify_interval_minutes=int_field("approval_notify_interval_minutes", 360, minimum=low,
                                                       maximum=high),
            approval_notify_daily_hour=int_field("approval_notify_daily_hour", 8, minimum=0, maximum=23),
        )
    elif action == "save_wiki_upload_policy":
        settings.update(global_wiki_upload_max_size_mb=int_field("global_wiki_upload_max_size_mb", 100, minimum=1, maximum=2048),
                        global_wiki_blocked_extensions=(request.form.get("global_wiki_blocked_extensions") or "").strip()[:2000])
        if _apply_to_all_running():
            flash(t("hosting.admin.some_restarts_failed"), "error")
    elif action == "save_grace_period":
        settings.update(grace_period_days=int_field("grace_period_days", 30, minimum=0, maximum=3650))
    elif action == "save_activation":
        was = settings.approval_required()
        activation = _checked("hosting_activation_required")
        timeout = seconds_from_form("act_timeout") if request.form.get("hosting_activation_denied_deletion_enabled") else -1
        settings.update(hosting_activation_required=activation, hosting_activation_denied_timeout_seconds=timeout,
                        signup_use_case_required=_checked("signup_use_case_required"))
        if was and not settings.approval_required():
            count = accounts.approve_all_pending(account()["id"])
            if count:
                flash(t("hosting.admin.auto_approved", count=count), "info")
    elif action == "save_instance_url_suffix":
        suffix = (request.form.get("instance_url_suffix") or "").strip().lower()
        from ..config import valid_suffix

        if suffix and not valid_suffix(suffix):
            raise ServiceError("hosting.admin.invalid_suffix")
        settings.update(instance_url_suffix=suffix or "hosting", instance_suffix_disabled=_checked("instance_suffix_disabled"))
        instances.sync_routes()
    elif action == "save_tour":
        settings.update(global_tour_enabled=_checked("global_tour_enabled"))
    elif action == "save_oauth":
        settings.update(platform_oauth_enabled=_checked("platform_oauth_enabled"))
    elif action == "save_api":
        settings.update(api_enabled=_checked("api_enabled"))
    elif action == "save_admin_mfa":
        if request.form.get("admin_mfa_required") and not account().get("totp_enabled"):
            raise ServiceError("hosting.mfa.enable_yours_first")
        settings.update(admin_mfa_required=_checked("admin_mfa_required"))
    elif action == "save_expired_wiki_permissions":
        settings.update(allow_owner_delete_expired=_checked("allow_owner_delete_expired"),
                        allow_owner_download_expired=_checked("allow_owner_download_expired"),
                        email_flag_block_reentry=_checked("email_flag_block_reentry"))
    elif action == "save_tts_policy":
        mode = request.form.get("global_tts_mode", "all")
        settings.update(global_tts_enabled=_checked("global_tts_enabled"),
                        global_tts_mode=mode if mode in settings.TTS_MODES else "all",
                        global_tts_list=(request.form.get("global_tts_list") or "").strip()[:4000])
    elif action == "save_tts_gpu":
        url = (request.form.get("global_tts_gpu_url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise ServiceError("hosting.admin.invalid_url")
        values: dict[str, Any] = {"global_tts_gpu_enabled": _checked("global_tts_gpu_enabled"),
                                  "global_tts_gpu_url": url,
                                  "global_tts_gpu_timeout": int_field("global_tts_gpu_timeout", 120, minimum=5, maximum=3600)}
        token = request.form.get("global_tts_gpu_auth_token") or ""
        if token or request.form.get("clear_token"):
            values["global_tts_gpu_auth_token"] = token.strip()
        settings.update(**values)
    elif action == "gdrive_save":
        backup_time = (request.form.get("gdrive_backup_time") or "03:00").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", backup_time):
            backup_time = "03:00"
        settings.update(gdrive_backup_enabled=_checked("gdrive_backup_enabled"),
                        gdrive_folder_id=(request.form.get("gdrive_folder_id") or "").strip()[:200],
                        gdrive_retention_days=int_field("gdrive_retention_days", 7, minimum=1, maximum=365),
                        gdrive_backup_time=backup_time)
    else:
        raise ServiceError("hosting.admin.unknown_action")
    return "hosting.admin.settings_saved"


@bp.route("/admin/settings", methods=["GET", "POST"])
@bp.route("/global-settings", methods=["GET", "POST"])
@auth.admin_required
@rate_limit(20)
def settings_page():
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action in ("gdrive_test", "gdrive_backup_now", "gdrive_upload_credentials"):
                _gdrive(action)
            else:
                _done(_save_settings(action))
        except ServiceError as error:
            flash_error(error)
        except RuntimeFailure as error:
            flash(t(f"hosting.runtime.{error.code}"), "error")
        return redirect(url_for("admin.settings_page"))
    return render_template("hosting/admin/settings.html", settings=settings.load(),
                           gpu_token_set=bool(settings.get("global_tts_gpu_auth_token")),
                           suffix=urls.instance_suffix(), email_configured=notifications.configured(),
                           notify_interval=settings.NOTIFY_INTERVAL_MINUTES)


def _gdrive(action: str) -> None:
    runtime = instances.runtime()
    if action == "gdrive_test":
        flash(t("hosting.admin.gdrive_ok", detail=runtime.gdrive_test()), "success")
    elif action == "gdrive_backup_now":
        flash(t("hosting.admin.gdrive_uploaded", detail=runtime.gdrive_backup_now()), "success")
    else:
        upload = request.files.get("gdrive_credentials_file")
        content = upload.read(1024 * 1024) if upload else b""
        try:
            json.loads(content or b"")
        except ValueError as error:
            raise ServiceError("hosting.admin.invalid_json") from error
        settings.update(gdrive_credentials_path=runtime.store_gdrive_credentials(content))
        _done("hosting.admin.gdrive_credentials_saved")


@bp.route("/admin/spawn-demos", methods=["GET", "POST"])
@auth.admin_required
def spawn_demos():
    """Retired in 1.6 (seeded demo content host-side); kept as a redirect."""
    flash(t("hosting.admin.demos_retired"), "info")
    return _dashboard()


# ── Archives and platform backups ─────────────────────────────────────────────


def _upload_root() -> Path:
    root = Path(current_app.config["HOSTING"].archives.import_temp_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _upload_dir(upload_id: str, kind: str) -> tuple[Path, dict[str, Any]]:
    if not _UPLOAD_ID.match(upload_id or ""):
        abort(404)
    directory = _upload_root() / upload_id
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        abort(404)
    if meta.get("account_id") != account()["id"] or meta.get("kind") != kind:
        abort(404)
    return directory, meta


def _start_upload(kind: str, extra: dict[str, Any]):
    limits = current_app.config["HOSTING"].archives
    filename = os.path.basename((request.form.get("filename") or "").strip())
    try:
        size = int(request.form.get("size") or 0)
    except ValueError:
        size = 0
    suffixes = (".zip",) if kind == "import" else (".zip", ".bwenc")
    if not filename.lower().endswith(suffixes) or not 0 < size <= limits.import_max_bytes:
        return jsonify(ok=False, error=t("hosting.archives.invalid_upload")), 400
    upload_id = secrets.token_urlsafe(24)
    directory = _upload_root() / upload_id
    directory.mkdir(mode=0o700)
    meta = {"account_id": account()["id"], "kind": kind, "size": size, "filename": filename, "created_at": now_sql(),
            **extra}
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return jsonify(ok=True, upload_id=upload_id, chunk_size=limits.import_chunk_bytes)


def _append_chunk(upload_id: str, kind: str):
    directory, meta = _upload_dir(upload_id, kind)
    part = directory / "upload.part"
    current = part.stat().st_size if part.exists() else 0
    try:
        offset = int(request.form.get("offset") or 0)
    except ValueError:
        offset = -1
    chunk = request.files.get("chunk")
    if chunk is None or offset != current:
        return jsonify(ok=False, error=t("hosting.archives.bad_chunk"), received=current), 409
    data = chunk.read(current_app.config["HOSTING"].archives.import_chunk_bytes + 1)
    if current + len(data) > meta["size"]:
        return jsonify(ok=False, error=t("hosting.archives.invalid_upload")), 400
    with part.open("ab") as handle:
        handle.write(data)
    return jsonify(ok=True, received=current + len(data))


def _finish_upload(upload_id: str, kind: str, handler):
    directory, meta = _upload_dir(upload_id, kind)
    part = directory / "upload.part"
    try:
        if not part.exists() or part.stat().st_size != meta["size"]:
            return jsonify(ok=False, error=t("hosting.archives.incomplete")), 400
        message = handler(part, meta)
    except ServiceError as error:
        return jsonify(ok=False, error=t(error.key, **error.values)), 400
    except RuntimeFailure as error:
        return jsonify(ok=False, error=t(f"hosting.runtime.{error.code}")), 400
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return jsonify(ok=True, message=message, redirect=url_for("admin.dashboard"))


def _import(path: Path, meta: dict[str, Any]) -> str:
    archive = path.with_suffix(".zip")
    path.rename(archive)
    inst = instances.import_archive(account(), meta["subdomain"], meta["domain_mode"], archive, actor_id=account()["id"])
    return t("hosting.archives.imported", url=urls.instance_url(inst))


def _restore(path: Path, meta: dict[str, Any]) -> str:
    target = path.with_name(meta["filename"])
    path.rename(target)
    instances.runtime().restore_platform([target])
    events.record("account", account()["id"], "platform.restored", account()["id"])
    return t("hosting.archives.platform_restored")


@bp.route("/admin/instances/import", methods=["GET", "POST"])
@auth.admin_required
@rate_limit(5, 300)
def import_instance():
    if request.method == "GET":
        return render_template("hosting/admin/import.html",
                               chunk_size=current_app.config["HOSTING"].archives.import_chunk_bytes)
    upload = request.files.get("archive")
    if upload is None or not (upload.filename or "").lower().endswith(".zip"):
        flash(t("hosting.archives.invalid_upload"), "error")
        return redirect(url_for("admin.import_instance"))
    work = Path(tempfile.mkdtemp(prefix="bwh-import-", dir=_upload_root()))
    try:
        archive = work / "upload.zip"
        upload.save(archive)
        mode = request.form.get("domain_mode", "hosting")
        inst = instances.import_archive(account(), request.form.get("subdomain") or "",
                                        mode if mode in urls.DOMAIN_MODES else "hosting", archive, actor_id=account()["id"])
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("admin.import_instance"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _done("hosting.archives.imported", url=urls.instance_url(inst))
    return _dashboard()


@bp.post("/admin/instances/import/chunk/start")
@auth.admin_required
@rate_limit(20, 300)
def import_chunk_start():
    mode = request.form.get("domain_mode", "hosting")
    return _start_upload("import", {"subdomain": (request.form.get("subdomain") or "").strip().lower(),
                                    "domain_mode": mode if mode in urls.DOMAIN_MODES else "hosting"})


@bp.post("/admin/instances/import/chunk/<upload_id>")
@auth.admin_required
def import_chunk(upload_id: str):
    return _append_chunk(upload_id, "import")


@bp.post("/admin/instances/import/chunk/<upload_id>/complete")
@auth.admin_required
@rate_limit(20, 300)
def import_chunk_complete(upload_id: str):
    return _finish_upload(upload_id, "import", _import)


@bp.get("/admin/instances/import/chunk/<upload_id>/status")
@auth.admin_required
def import_chunk_status(upload_id: str):
    directory, meta = _upload_dir(upload_id, "import")
    part = directory / "upload.part"
    return jsonify(ok=True, received=part.stat().st_size if part.exists() else 0, size=meta["size"])


@bp.post("/admin/export-platform")
@auth.admin_required
@rate_limit(5)
def export_platform():
    root = Path(current_app.config["HOSTING"].archives.export_temp_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bwh-platform-", dir=root))
    try:
        path = instances.runtime().export_platform(work)
    except RuntimeFailure as error:
        shutil.rmtree(work, ignore_errors=True)
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return redirect(url_for("admin.settings_page"))
    events.record("account", account()["id"], "platform.exported", account()["id"])
    return stream_file(path, path.name, work)


@bp.post("/admin/export-backup-key")
@auth.admin_required
@rate_limit(3, 300)
def export_backup_key():
    if not accounts.verify_password(auth.real_account(), request.form.get("current_password") or ""):
        flash(t("hosting.account.wrong_password"), "error")
        return redirect(url_for("admin.settings_page"))
    try:
        key = instances.runtime().backup_key()
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return redirect(url_for("admin.settings_page"))
    events.record("account", account()["id"], "platform.backup_key_exported", account()["id"])
    return send_file(BytesIO(key), as_attachment=True, download_name="bananawiki-backup-encryption.key",
                     mimetype="application/octet-stream")


@bp.post("/admin/restore-platform")
@auth.admin_required
@rate_limit(5)
def restore_platform():
    files = [f for f in request.files.getlist("backup_files") if f.filename]
    if not files or not all(f.filename.lower().endswith((".zip", ".bwenc")) for f in files):
        flash(t("hosting.archives.invalid_upload"), "error")
        return redirect(url_for("admin.settings_page"))
    work = Path(tempfile.mkdtemp(prefix="bwh-restore-", dir=_upload_root()))
    try:
        paths = []
        for index, upload in enumerate(files):
            target = work / f"{index}-{os.path.basename(upload.filename)}"
            upload.save(target)
            paths.append(target)
        instances.runtime().restore_platform(paths)
    except RuntimeFailure as error:
        flash(t(f"hosting.runtime.{error.code}"), "error")
        return redirect(url_for("admin.settings_page"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _done("hosting.archives.platform_restored")
    return redirect(url_for("admin.settings_page"))


@bp.post("/admin/restore-platform/chunk/start")
@auth.admin_required
@rate_limit(20, 300)
def restore_chunk_start():
    return _start_upload("restore", {})


@bp.post("/admin/restore-platform/chunk/<upload_id>")
@auth.admin_required
def restore_chunk(upload_id: str):
    return _append_chunk(upload_id, "restore")


@bp.post("/admin/restore-platform/chunk/<upload_id>/complete")
@auth.admin_required
@rate_limit(20, 300)
def restore_chunk_complete(upload_id: str):
    return _finish_upload(upload_id, "restore", _restore)


@bp.get("/admin/restore-platform/chunk/<upload_id>/status")
@auth.admin_required
def restore_chunk_status(upload_id: str):
    directory, meta = _upload_dir(upload_id, "restore")
    part = directory / "upload.part"
    return jsonify(ok=True, received=part.stat().st_size if part.exists() else 0, size=meta["size"])


__all__ = ["bp"]
