"""The signed-in account: details, password, sessions, email, two-step sign-in,
API tokens, data export, deletion and account merges."""

from __future__ import annotations

import json
import secrets
import time

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ...core.crypto import constant_time_equals
from ...core.timeutil import sql_in
from ...core.web import safe_next
from .. import accounts, api_tokens, attention, auth, instances, merges, mfa, notifications, settings
from ..errors import ServiceError
from ..i18n import t
from ..limits import rate_limit
from .common import account, flash_error

bp = Blueprint("account", __name__)

TOKEN_NONCE_KEY = "hosting_api_token_form_nonce"
ENROLLMENT_KEY = "mfa_enrollment"
ENROLLMENT_AT_KEY = "mfa_enrollment_at"


def _no_impersonation() -> None:
    if auth.impersonating():
        abort(403)


def _render_account(**extra):
    current = account()
    impersonated = auth.impersonating()
    approved = current["approval_status"] == "approved"
    tokens = api_tokens.list_for(current["id"]) if approved else []
    api_enabled = settings.flag("api_enabled")
    nonce = None
    if approved and api_enabled and not impersonated:
        nonce = session.get(TOKEN_NONCE_KEY) or secrets.token_urlsafe(24)
        session[TOKEN_NONCE_KEY] = nonce
    current_id = auth.current_session_id()
    return render_template(
        "hosting/account/account.html",
        sessions=[] if impersonated else auth.active_sessions(current["id"]),
        history=[] if impersonated else auth.session_history(current["id"]),
        current_session_id=current_id, tokens=tokens, api_enabled=api_enabled,
        show_tokens=approved and (api_enabled or bool(tokens)), token_nonce=nonce,
        token_scopes=api_tokens.scopes_for(current), token_limit=api_tokens.MAX_ACTIVE,
        email_required=settings.flag("email_required"), cooldown=accounts.cooldown_remaining(current),
        email_configured=notifications.configured(), merges=merges.for_account(current["id"]), **extra,
    )


@bp.route("/account", methods=["GET", "POST"], endpoint="account")
@auth.exempt("verification", "approval", "contact_email")
@rate_limit(20)
def account_page():
    if request.method == "GET":
        return _render_account()
    current = account()
    try:
        if not accounts.verify_password(current, request.form.get("current_password") or ""):
            identity_changed = ((request.form.get("username") or "").strip() != current["username"]
                                or (request.form.get("email") or "").strip().lower() != (current["email"] or ""))
            if identity_changed:
                raise ServiceError("hosting.account.password_required")
        email_changed = accounts.update_identity(current, username=request.form.get("username") or "",
                                                 email=request.form.get("email") or "",
                                                 email_required=settings.flag("email_required"))
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.account"))
    auth.refresh()
    if email_changed and notifications.verification_required():
        ok, key = notifications.send_verification(accounts.get(current["id"]), ignore_cooldown=True)  # type: ignore[arg-type]
        flash(t(key), "success" if ok else "error")
        return redirect(url_for("account.verify_email_pending"))
    flash(t("hosting.account.saved"), "success")
    return redirect(url_for("account.account"))



@bp.post("/account/change-password")
@bp.post("/settings/change-password")
@rate_limit(5)
def change_password():
    current = account()
    _no_impersonation()
    if not accounts.verify_password(current, request.form.get("current_password") or ""):
        flash(t("hosting.account.wrong_password"), "error")
        return redirect(url_for("account.account"))
    try:
        new = accounts.check_new_password(request.form.get("new_password") or "",
                                          request.form.get("confirm_new_password") or "")
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.account"))
    version = accounts.set_password(current["id"], new, actor_id=current["id"], reason="Password changed")
    auth.keep_current_session(current["id"], version)
    flash(t("hosting.account.password_changed"), "success")
    return redirect(url_for("account.account"))


@bp.post("/account/delete")
@bp.post("/settings/delete")
@auth.exempt(*auth.GATES)
@rate_limit(5)
def delete_account():
    current = account()
    _no_impersonation()
    if not accounts.verify_password(current, request.form.get("current_password") or ""):
        flash(t("hosting.account.wrong_password"), "error")
        return redirect(url_for("account.account"))
    if current["is_admin"] and accounts.count_active_admins(exclude=current["id"]) == 0:
        flash(t("hosting.account.last_admin"), "error")
        return redirect(url_for("account.account"))
    try:
        accounts.check_deletable(current["id"])
        for inst in instances.owned_by(current["id"]):
            instances.terminate(inst, actor_id=current["id"], reason="account_deleted")
        accounts.delete(current["id"])
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.account"))
    auth.end_session()
    flash(t("hosting.account.deleted"), "success")
    return redirect(url_for("auth.login"))


# ── Sessions ──────────────────────────────────────────────────────────────────


@bp.post("/account/sessions/<session_id>/revoke")
@rate_limit(20)
def revoke_session(session_id: str):
    current = account()
    _no_impersonation()
    is_current = session_id == auth.current_session_id()
    if not auth.revoke_session(current["id"], session_id[:128]):
        abort(404)
    if is_current:
        auth.end_session()
        return redirect(url_for("auth.login"))
    flash(t("hosting.account.sessions.revoked"), "success")
    return redirect(url_for("account.account") + "#sessions")


@bp.post("/account/sessions/logout-all")
@auth.exempt(*auth.GATES)
@rate_limit(10)
def logout_everywhere():
    current = account()
    _no_impersonation()
    auth.revoke_all(current["id"])
    accounts.bump_session_version(current["id"])
    auth.end_session()
    flash(t("hosting.account.sessions.everywhere"), "info")
    return redirect(url_for("auth.login"))


@bp.post("/account/sessions/history/clear")
@rate_limit(10)
def clear_session_history():
    _no_impersonation()
    auth.clear_history(account()["id"])
    flash(t("hosting.account.sessions.history_cleared"), "success")
    return redirect(url_for("account.account") + "#sessions")


# ── Email ─────────────────────────────────────────────────────────────────────


@bp.route("/account/contact-email", methods=["GET", "POST"])
@auth.exempt("contact_email", "verification", "approval")
@rate_limit(8, 300)
def contact_email():
    current = account()
    if request.method == "GET":
        if not auth.contact_email_required(current):
            return redirect(url_for("dashboard.dashboard"))
        return render_template("hosting/account/contact_email.html")
    try:
        if not accounts.verify_password(current, request.form.get("current_password") or ""):
            raise ServiceError("hosting.account.wrong_password")
        accounts.set_contact_email(current, request.form.get("email") or "")
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.contact_email"))
    auth.refresh()
    if notifications.verification_required():
        ok, key = notifications.send_verification(accounts.get(current["id"]), ignore_cooldown=True)  # type: ignore[arg-type]
        flash(t(key), "success" if ok else "error")
        return redirect(url_for("account.verify_email_pending"))
    flash(t("hosting.account.contact_saved"), "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.get("/account/verify-email")
@auth.exempt("verification", "approval", "contact_email")
def verify_email_pending():
    current = account()
    if current.get("email_verified_at") or not current.get("email"):
        return redirect(url_for("dashboard.dashboard"))
    return render_template("hosting/account/verify_email.html", cooldown=accounts.cooldown_remaining(current),
                           email_configured=notifications.configured())


@bp.post("/account/resend-verification")
@auth.exempt("verification", "approval", "contact_email")
@rate_limit(5, 300)
def resend_verification():
    current = account()
    if current.get("email_verified_at"):
        flash(t("hosting.verify.already"), "info")
        return redirect(url_for("account.account"))
    ok, key = notifications.send_verification(current)
    flash(t(key, seconds=accounts.cooldown_remaining(current)), "success" if ok else "error")
    return redirect(url_for("account.verify_email_pending"))


@bp.route("/account/email-flagged", methods=["GET", "POST"])
@auth.exempt("email_flag", "contact_email", "verification", "approval")
@rate_limit(8, 300)
def email_flagged():
    current = account()
    if not current.get("email_flagged_invalid"):
        return redirect(url_for("dashboard.dashboard"))
    if request.method == "POST":
        try:
            accounts.replace_flagged_email(current, request.form.get("email") or "")
        except ServiceError as error:
            flash_error(error)
            return redirect(url_for("account.email_flagged"))
        auth.refresh()
        if notifications.verification_required():
            ok, key = notifications.send_verification(accounts.get(current["id"]), ignore_cooldown=True)  # type: ignore[arg-type]
            flash(t(key), "success" if ok else "error")
            if ok:
                return redirect(url_for("account.verify_email_pending"))
        flash(t("hosting.email_flagged.saved"), "success")
        return redirect(url_for("dashboard.dashboard"))
    return render_template("hosting/account/email_flagged.html",
                           reason=current["email_flag_reason"] if current["email_flag_reason_visible"] else "")


@bp.post("/account/export")
@rate_limit(3, 300)
def export_account():
    data = accounts.export_data(account())
    response = Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=bananawiki-account-data.json"
    return response


# ── Two-step sign-in ──────────────────────────────────────────────────────────


@bp.route("/account/mfa", methods=["GET", "POST"], endpoint="mfa")
@auth.exempt("admin_mfa")
@rate_limit(10)
def mfa_settings():
    current = account()
    _no_impersonation()
    enabled = bool(current.get("totp_enabled"))
    if not enabled and (not session.get(ENROLLMENT_KEY) or time.time() - float(session.get(ENROLLMENT_AT_KEY, 0)) > 600):
        session[ENROLLMENT_KEY] = mfa.encrypt_secret(mfa.generate_secret())
        session[ENROLLMENT_AT_KEY] = time.time()
    secret = "" if enabled else mfa.decrypt_secret(session.get(ENROLLMENT_KEY))
    if request.method == "POST":
        code = request.form.get("code", "")[:128]
        if not accounts.verify_password(current, request.form.get("password") or ""):
            flash(t("hosting.account.wrong_password"), "error")
        elif enabled:
            if mfa.verify_second_factor(current["id"], code):
                mfa.disable(current["id"])
                auth.revoke_all(current["id"])
                auth.start_session(accounts.get(current["id"]), "password")  # type: ignore[arg-type]
                flash(t("hosting.mfa.disabled"), "success")
                return redirect(url_for("account.account"))
            flash(t("hosting.mfa.invalid_code"), "error")
        else:
            counter = mfa.matching_counter(secret, code)
            if counter is None:
                flash(t("hosting.mfa.invalid_code"), "error")
            else:
                codes = mfa.enable(current["id"], secret, counter)
                if codes is None:
                    abort(409)
                session.pop(ENROLLMENT_KEY, None)
                auth.revoke_all(current["id"])
                auth.start_session(accounts.get(current["id"]), "totp")  # type: ignore[arg-type]
                response = make_response(render_template("hosting/account/mfa_recovery.html", codes=codes))
                response.headers["Cache-Control"] = "no-store"
                return response
    qr = mfa.qr_data_uri(mfa.provisioning_uri(secret, current["username"])) if secret else ""
    return render_template("hosting/account/mfa.html", enabled=enabled, secret=secret, qr=qr)



# ── API tokens ────────────────────────────────────────────────────────────────


@bp.get("/account/api-tokens")
def api_tokens_page():
    return redirect(url_for("account.account") + "#api-tokens")


@bp.post("/account/api-tokens")
@rate_limit(5)
def create_api_token():
    current = account()
    if not settings.flag("api_enabled"):
        abort(404)
    if auth.impersonating():
        flash(t("hosting.api_tokens.impersonation"), "error")
        return redirect(url_for("account.account") + "#api-tokens")
    expected = session.get(TOKEN_NONCE_KEY) or ""
    if not expected or not constant_time_equals(expected, request.form.get("form_nonce") or ""):
        flash(t("hosting.api_tokens.form_used"), "info")
        return redirect(url_for("account.account") + "#api-tokens")
    if not accounts.verify_password(current, request.form.get("current_password") or ""):
        flash(t("hosting.account.wrong_password"), "error")
        return redirect(url_for("account.account") + "#api-tokens")
    expiry = request.form.get("expires_in") or ""
    if expiry not in api_tokens.EXPIRY_CHOICES:
        flash(t("hosting.api_tokens.invalid_expiry"), "error")
        return redirect(url_for("account.account") + "#api-tokens")
    days = api_tokens.EXPIRY_CHOICES[expiry]
    session.pop(TOKEN_NONCE_KEY, None)
    try:
        _token_id, raw = api_tokens.create(current, request.form.get("name") or "", request.form.getlist("scopes"),
                                           sql_in(days=days) if days else None,
                                           int(current.get("session_version") or 0))
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.account") + "#api-tokens")
    response = make_response(_render_account(new_token=raw))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/account/api-tokens/<int:token_id>/revoke")
@auth.exempt("verification")
@rate_limit(20)
def revoke_api_token(token_id: int):
    current = account()
    actor = auth.real_account() or current
    if not api_tokens.revoke(current["id"], token_id, actor["id"]):
        abort(404)
    flash(t("hosting.api_tokens.revoked"), "success")
    return redirect(url_for("account.account") + "#api-tokens")


# ── Account merges ────────────────────────────────────────────────────────────


@bp.route("/account/merge-request", methods=["GET", "POST"])
@rate_limit(10, 300)
def merge_request():
    if request.method == "POST":
        try:
            merges.request(account(), request.form.get("target_username") or "", request.form.get("reason") or "")
        except ServiceError as error:
            flash_error(error)
            return render_template("hosting/account/merge_request.html"), 400
        flash(t("hosting.merges.requested"), "success")
        return redirect(url_for("account.merge_pending"))
    return render_template("hosting/account/merge_request.html")


@bp.get("/account/merge/pending")
def merge_pending():
    return render_template("hosting/account/merge_pending.html", merges=merges.for_account(account()["id"]))


@bp.post("/account/merge/approve/<int:merge_id>")
@rate_limit(10)
def merge_approve(merge_id: int):
    try:
        merges.approve(merge_id, account())
        flash(t("hosting.merges.approved"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("account.merge_pending"))


@bp.post("/account/merge/cancel/<int:merge_id>")
@rate_limit(10)
def merge_cancel(merge_id: int):
    try:
        merges.close(merge_id, account(), "cancelled")
        flash(t("hosting.merges.cancelled"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("account.merge_pending"))


# ── Notifications ─────────────────────────────────────────────────────────────


@bp.post("/account/notifications")
@rate_limit(20)
def notification_preferences():
    """Administrators choose whether they get emails about requests waiting for them."""
    _no_impersonation()
    current = account()
    accounts.set_attention_emails(current["id"], bool(request.form.get("attention_emails")))
    flash(t("hosting.account.saved"), "success")
    return redirect(url_for("account.account") + "#notifications")


@bp.post("/notices/<int:notice_id>/dismiss")
@auth.exempt("verification", "approval", "contact_email")
def dismiss_notice(notice_id: int):
    if not attention.dismiss(account()["id"], notice_id):
        abort(404)
    return redirect(safe_next(url_for("dashboard.dashboard"), request.form.get("next")))


@bp.post("/notices/dismiss-all")
@auth.exempt("verification", "approval", "contact_email")
def dismiss_notices():
    attention.dismiss(account()["id"])
    return redirect(safe_next(url_for("dashboard.dashboard"), request.form.get("next")))
