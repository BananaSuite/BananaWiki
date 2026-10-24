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
from .. import accounts, api_tokens, attention, auth, instances, merges, mfa, notifications, oauth, settings
from ..errors import ServiceError
from ..i18n import t
from ..limits import rate_limit
from .common import account, flash_error

bp = Blueprint("account", __name__)

TOKEN_NONCE_KEY = "hosting_api_token_form_nonce"
ENROLLMENT_KEY = "mfa_enrollment"
ENROLLMENT_AT_KEY = "mfa_enrollment_at"
# The account page is open while an account waits for approval, verification, a contact address or new
# terms; what it offers for the account's own security works then too, so its forms never bounce.
ACCOUNT_PAGE_GATES = ("verification", "approval", "contact_email", "terms")


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
        email_required=accounts.address_required(current), cooldown=accounts.cooldown_remaining(current),
        email_configured=notifications.configured(), merges=merges.for_account(current["id"]),
        show_wiki_signins=oauth.enabled(), **extra,
    )


@bp.route("/account", methods=["GET", "POST"], endpoint="account")
@auth.exempt(*ACCOUNT_PAGE_GATES)
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
        old_address = current.get("email") or ""
        email_changed = accounts.update_identity(current, username=request.form.get("username") or "",
                                                 email=request.form.get("email") or "",
                                                 email_required=accounts.address_required(current))
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.account"))
    auth.refresh()
    if email_changed and notifications.verification_required():
        ok, key = notifications.send_verification(accounts.get(current["id"]), ignore_cooldown=True)  # type: ignore[arg-type]
        flash(t(key), "success" if ok else "error")
        notifications.email_changed(current, old_address, by_account=True)
        return redirect(url_for("account.verify_email_pending"))
    if email_changed:
        notifications.email_changed(current, old_address, by_account=True)
    flash(t("hosting.account.saved"), "success")
    return redirect(url_for("account.account"))



@bp.post("/account/change-password")
@bp.post("/settings/change-password")
@auth.exempt(*ACCOUNT_PAGE_GATES)
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
    version = accounts.set_password(current["id"], new, actor_id=current["id"], reason="Password changed",
                                    keep_session_id=auth.current_session_id())
    auth.keep_current_session(current["id"], version)
    notifications.password_changed(current["id"], by_account=True)
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
        instances.terminate_all(current["id"], actor_id=current["id"])
        accounts.delete(current["id"])
    except ServiceError as error:
        if error.key == "hosting.accounts.owns_main_wiki" and not current["is_admin"]:
            # Whether a wiki is a main wiki is for administrators to know; the owner only hears that it is kept.
            flash(t("hosting.account.delete_kept_wiki", slug=error.values.get("slug", "")), "error")
        else:
            flash_error(error)
        return redirect(url_for("account.account"))
    auth.end_session()
    flash(t("hosting.account.deleted"), "success")
    return redirect(url_for("auth.login"))


# Sessions


@bp.post("/account/sessions/<session_id>/revoke")
@auth.exempt(*ACCOUNT_PAGE_GATES)
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
    """"Sign out other devices": every other session ends, this one stays signed in."""
    current = account()
    _no_impersonation()
    version = accounts.bump_session_version(current["id"])
    auth.keep_current_session(current["id"], version)
    flash(t("hosting.account.sessions.everywhere"), "success")
    return redirect(url_for("account.account") + "#sessions")


# Wikis the account signs in to (platform sign-in)


def _platform_sign_in() -> None:
    if not oauth.enabled():
        abort(404)


@bp.get("/account/wikis")
@auth.exempt(*ACCOUNT_PAGE_GATES)
def wiki_signins():
    """The wikis this account signs in to with the portal: open, sign out of one or all, withdraw consent."""
    _platform_sign_in()
    current = account()
    return render_template("hosting/account/wikis.html", wikis=oauth.connected_wikis(current["id"]),
                           can_act=not auth.impersonating())


@bp.post("/account/wikis/<instance_id>/sign-out")
@auth.exempt(*auth.GATES)  # signing out only takes access away: never held up by an account state
@rate_limit(20)
def wiki_sign_out(instance_id: str):
    _platform_sign_in()
    _no_impersonation()
    if oauth.sign_out_of(account()["id"], instance_id[:64]):
        flash(t("hosting.wikis.signed_out"), "success")
    else:
        flash(t("hosting.wikis.not_signed_in"), "info")
    return redirect(url_for("account.wiki_signins"))


@bp.post("/account/wikis/<instance_id>/remove")
@auth.exempt(*auth.GATES)
@rate_limit(20)
def wiki_remove_access(instance_id: str):
    _platform_sign_in()
    _no_impersonation()
    if oauth.remove_access(account()["id"], instance_id[:64]):
        flash(t("hosting.wikis.removed"), "success")
    else:
        flash(t("hosting.wikis.not_signed_in"), "info")
    return redirect(url_for("account.wiki_signins"))


@bp.post("/account/sessions/history/clear")
@auth.exempt(*ACCOUNT_PAGE_GATES)
@rate_limit(10)
def clear_session_history():
    _no_impersonation()
    auth.clear_history(account()["id"])
    flash(t("hosting.account.sessions.history_cleared"), "success")
    return redirect(url_for("account.account") + "#sessions")


# Email


@bp.route("/account/contact-email", methods=["GET", "POST"])
@auth.exempt("contact_email", "verification", "approval")
@rate_limit(8, 300)
def contact_email():
    """Add an address: required (a gate) or, once, suggested after signing in (it may be skipped)."""
    current = account()
    required = auth.contact_email_required(current)
    next_url = safe_next(url_for("dashboard.dashboard"), request.values.get("next"))
    if not required and not auth.contact_email_suggested(current):
        return redirect(next_url)
    if request.method == "GET":
        return render_template("hosting/account/contact_email.html", required=required,
                               next_url=request.values.get("next") or "")
    if request.form.get("action") == "skip":
        if not required and not auth.impersonating():
            accounts.dismiss_email_prompt(current["id"])
        return redirect(next_url)
    try:
        if not accounts.verify_password(current, request.form.get("current_password") or ""):
            raise ServiceError("hosting.account.wrong_password")
        accounts.set_contact_email(current, request.form.get("email") or "")
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("account.contact_email", next=request.values.get("next") or None))
    auth.refresh()
    if notifications.verification_required():
        ok, key = notifications.send_verification(accounts.get(current["id"]), ignore_cooldown=True)  # type: ignore[arg-type]
        flash(t(key), "success" if ok else "error")
        return redirect(url_for("account.verify_email_pending"))
    flash(t("hosting.account.contact_saved"), "success")
    return redirect(next_url)


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
            # As on the account page: a new address (and so a way to reset the password) needs the password.
            if not accounts.verify_password(current, request.form.get("current_password") or ""):
                raise ServiceError("hosting.account.wrong_password")
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
@auth.exempt("terms")
@rate_limit(3, 300)
def export_account():
    data = accounts.export_data(account())
    response = Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=bananawiki-account-data.json"
    return response


# Terms of service


@bp.route("/account/terms", methods=["GET", "POST"], endpoint="terms")
@auth.exempt("terms")
@rate_limit(10)
def accept_terms():
    """New terms the operator requires every account to accept (``HOSTING_TERMS_REQUIRED_VERSION``).

    Whoever does not accept them can still download their data, delete
    their account (on the account page) or sign out.
    """
    current = account()
    if not accounts.terms_outdated(current) or auth.impersonating():
        return redirect(url_for("dashboard.dashboard"))
    if request.method == "POST":
        if request.form.get("accept_terms") != "1":
            flash(t("hosting.signup.terms_required"), "error")
            return render_template("hosting/account/terms.html", version=accounts.terms_version()), 400
        accounts.accept_terms(current)
        auth.refresh()
        gate = auth.pending_gate(accounts.get(current["id"]) or current, None)
        return redirect(url_for(gate or "dashboard.dashboard"))
    return render_template("hosting/account/terms.html", version=accounts.terms_version())


# Two-step sign-in


@bp.route("/account/mfa", methods=["GET", "POST"], endpoint="mfa")
@auth.exempt("admin_mfa", *ACCOUNT_PAGE_GATES)
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
                updated = accounts.get(current["id"])
                auth.start_session(updated, "password")  # type: ignore[arg-type]
                flash(t("hosting.mfa.disabled"), "success")
                response = redirect(url_for("account.account"))
                auth.set_device_cookie(response, updated)  # type: ignore[arg-type]  # the epoch moved on
                return response
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
                updated = accounts.get(current["id"])
                auth.start_session(updated, "totp")  # type: ignore[arg-type]
                response = make_response(render_template("hosting/account/mfa_recovery.html", codes=codes))
                response.headers["Cache-Control"] = "no-store"
                # This browser passed both steps: it stays known for the new epoch.
                auth.set_device_cookie(response, updated)  # type: ignore[arg-type]
                return response
    qr = mfa.qr_data_uri(mfa.provisioning_uri(secret, current["username"])) if secret else ""
    return render_template("hosting/account/mfa.html", enabled=enabled, secret=secret, qr=qr,
                           required=auth.admin_mfa_missing(current))



# API tokens


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
@auth.exempt(*auth.GATES)  # revoking only takes access away: never held up by an account state
@rate_limit(20)
def revoke_api_token(token_id: int):
    current = account()
    actor = auth.real_account() or current
    if not api_tokens.revoke(current["id"], token_id, actor["id"]):
        abort(404)
    flash(t("hosting.api_tokens.revoked"), "success")
    return redirect(url_for("account.account") + "#api-tokens")


# Account merges


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


# Notifications


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
@auth.exempt(*auth.GATES)  # hiding a notice changes nothing else: possible from any page it shows on
def dismiss_notice(notice_id: int):
    if not attention.dismiss(account()["id"], notice_id):
        abort(404)
    return redirect(safe_next(url_for("dashboard.dashboard"), request.form.get("next")))


@bp.post("/notices/dismiss-all")
@auth.exempt(*auth.GATES)
def dismiss_notices():
    attention.dismiss(account()["id"])
    return redirect(safe_next(url_for("dashboard.dashboard"), request.form.get("next")))
