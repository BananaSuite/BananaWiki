"""Sign-in, two-step sign-in, sign-up, account recovery and account-state pages."""

from __future__ import annotations

import hashlib
import hmac
import time

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from ...core.web import safe_next
from .. import accounts, auth, mfa, notifications, settings
from ..errors import ServiceError
from ..i18n import t
from ..limits import clear, exceeded, hit, rate_limit, record
from .common import flash_error

bp = Blueprint("auth", __name__)

LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_IP = 20
LOGIN_MAX_PER_ACCOUNT = 8
MFA_PENDING_KEY = "hosting_mfa_pending_id"
MFA_STARTED_KEY = "hosting_mfa_started_at"
MFA_VERSION_KEY = "hosting_mfa_session_version"
MFA_NEXT_KEY = "hosting_mfa_next"
MFA_SECONDS = 300
_MAX_FORM_AGE = 4 * 3600


# ── Bot protection (signed form timestamp and honeypot, as in 1.4) ────────────


def _sign(timestamp: str) -> str:
    key = current_app.config["HOSTING"].secret_key.encode("utf-8")
    return hmac.new(key, b"form-time:" + timestamp.encode("ascii"), hashlib.sha256).hexdigest()[:20]


def form_token() -> str:
    stamp = str(int(time.time()))
    return f"{stamp}.{_sign(stamp)}"


def bot_blocked() -> bool:
    if not settings.flag("bot_protection_enabled", True):
        return False
    if request.form.get("website"):
        return True
    stamp, _, signature = (request.form.get("_form_time") or "").partition(".")
    if not stamp.isdigit() or not hmac.compare_digest(signature, _sign(stamp)):
        return True
    age = time.time() - int(stamp)
    return age < current_app.config["HOSTING"].min_form_seconds or age > _MAX_FORM_AGE


def _redirect_if_signed_in():
    return redirect(url_for("dashboard.dashboard")) if auth.current_account() else None


# ── Sign-in ───────────────────────────────────────────────────────────────────


def complete_login(account: dict, next_url: str | None, method: str):
    """Open a session once every factor passed and send the account where it must go."""
    auth.start_session(account, method)
    clear("login-failed")
    gate = auth.pending_gate(account, None)
    if gate:
        return redirect(url_for(gate))
    if settings.flag("ask_email_existing_users") and not account.get("email") and not account.get("email_prompt_dismissed"):
        return redirect(url_for("account.account"))
    return redirect(safe_next(url_for("dashboard.dashboard"), next_url))


@bp.route("/login", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
def login():
    signed_in = _redirect_if_signed_in()
    if signed_in:
        return signed_in
    next_url = request.values.get("next") or ""
    if request.method == "GET":
        return render_template("hosting/auth/login.html", form_time=form_token(), next_url=next_url)
    username = (request.form.get("username") or "").strip()[:60]
    password = request.form.get("password") or ""

    def refuse(key: str, status: int):
        flash(t(key), "error")
        return render_template("hosting/auth/login.html", form_time=form_token(), next_url=next_url,
                               username=username), status

    if (exceeded("login-failed", LOGIN_MAX_PER_IP, LOGIN_WINDOW)
            or exceeded("login-account", LOGIN_MAX_PER_ACCOUNT, LOGIN_WINDOW, key=username.lower())):
        return refuse("hosting.login.too_many", 429)
    if bot_blocked():
        return refuse("hosting.form.rejected", 400)
    account = accounts.by_username(username)
    if not accounts.verify_password(account, password):
        record("login-failed")
        record("login-account", key=username.lower())
        return refuse("hosting.login.invalid", 401)
    if accounts.is_suspended(account):  # type: ignore[arg-type]
        session[auth.SUSPENDED_KEY] = account["id"]  # type: ignore[index]
        return redirect(url_for("auth.account_suspended"))
    if account.get("totp_enabled"):  # type: ignore[union-attr]
        session.clear()
        session[MFA_PENDING_KEY] = account["id"]  # type: ignore[index]
        session[MFA_STARTED_KEY] = time.time()
        session[MFA_VERSION_KEY] = int(account.get("session_version") or 0)  # type: ignore[union-attr]
        session[MFA_NEXT_KEY] = next_url
        return redirect(url_for("auth.login_mfa"))
    return complete_login(account, next_url, "password")  # type: ignore[arg-type]


@bp.route("/login/mfa", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(10)
def login_mfa():
    account = accounts.get(session.get(MFA_PENDING_KEY))
    if (not account or not account.get("totp_enabled") or account["deleted_at"]
            or time.time() - float(session.get(MFA_STARTED_KEY, 0)) > MFA_SECONDS
            or session.get(MFA_VERSION_KEY) != int(account.get("session_version") or 0)):
        session.pop(MFA_PENDING_KEY, None)
        flash(t("hosting.mfa.sign_in_again"), "info")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if not hit("mfa-login", 10, 300, key=account["id"]):
            abort(429)
        method = mfa.verify_second_factor(account["id"], request.form.get("code", ""))
        if method:
            if accounts.is_suspended(account):
                session[auth.SUSPENDED_KEY] = account["id"]
                return redirect(url_for("auth.account_suspended"))
            return complete_login(account, session.get(MFA_NEXT_KEY), method)
        flash(t("hosting.mfa.invalid_code"), "error")
    return render_template("hosting/auth/login_mfa.html")


@bp.post("/logout")
@auth.public
@auth.exempt(*auth.GATES)
def logout():
    auth.end_session()
    return redirect(url_for("auth.login"))


# ── Sign-up ───────────────────────────────────────────────────────────────────


def _bootstrap_allowed() -> bool | None:
    """None: first signup is locked (no token configured); False: wrong token."""
    expected = current_app.config["HOSTING"].bootstrap_token
    if not expected:
        return None
    supplied = (request.values.get("bootstrap_token") or "").strip()
    return hmac.compare_digest(expected.encode(), supplied.encode())


@bp.route("/signup", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(5)
def signup():
    signed_in = _redirect_if_signed_in()
    if signed_in:
        return signed_in
    from ..db import db

    first = not db.scalar("SELECT 1 FROM accounts LIMIT 1")
    if first:
        allowed = _bootstrap_allowed()
        if allowed is None:
            return render_template("hosting/auth/bootstrap_locked.html"), 503
        if not allowed:
            abort(404)
    mode = "open" if first else settings.signup_mode()
    approval = not first and settings.approval_required()
    context = {
        "first": first, "mode": mode, "approval": approval, "form_time": form_token(),
        "ask_email": settings.flag("ask_email_new_signup"),
        "email_required": settings.flag("ask_email_new_signup") and settings.flag("email_required"),
        "use_case_required": approval and settings.flag("signup_use_case_required"),
        "bootstrap_token": request.values.get("bootstrap_token", "") if first else "", "form": request.form,
    }
    if request.method == "GET" or mode == "closed":
        return render_template("hosting/auth/signup.html", **context), 403 if mode == "closed" and request.method == "POST" else 200
    try:
        if bot_blocked():
            raise ServiceError("hosting.form.rejected")
        if request.form.get("accept_terms") != "1":
            raise ServiceError("hosting.signup.terms_required")
        email = request.form.get("email") or ""
        if context["email_required"] and not email.strip():
            raise ServiceError("hosting.accounts.email_required")
        use_case = (request.form.get("signup_use_case") or "").strip()
        if context["use_case_required"] and not 20 <= len(use_case) <= 2000:
            raise ServiceError("hosting.signup.use_case_length")
        accounts.check_new_password(request.form.get("password") or "", request.form.get("confirm_password") or "")
        account = accounts.signup(request.form.get("username") or "", request.form.get("password") or "",
                                  email=email, invite_code=request.form.get("invite_code") or "", use_case=use_case,
                                  bootstrap_allowed=first)
    except ServiceError as error:
        flash_error(error)
        return render_template("hosting/auth/signup.html", **context), 400
    auth.start_session(account, "signup")
    if account["email"] and notifications.verification_required():
        ok, key = notifications.send_verification(account, ignore_cooldown=True)
        flash(t(key), "success" if ok else "error")
    if first:
        flash(t("hosting.signup.first_admin"), "success")
    elif account["approval_status"] == "pending":
        flash(t("hosting.signup.pending"), "success")
    else:
        flash(t("hosting.signup.created"), "success")
    gate = auth.pending_gate(account, None)
    return redirect(url_for(gate or "dashboard.dashboard"))


# ── Recovery ──────────────────────────────────────────────────────────────────


@bp.route("/forgot-username", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(5)
def forgot_username():
    signed_in = _redirect_if_signed_in()
    if signed_in:
        return signed_in
    if request.method == "POST":
        address = (request.form.get("email") or "").strip().lower()
        if not accounts.EMAIL.match(address):
            flash(t("hosting.accounts.invalid_email"), "error")
        else:
            account = accounts.by_email(address)
            if account and account.get("email_verified_at") and notifications.configured():
                notifications.send(account["email"], "username", username=account["username"],
                                   detail_value=account["username"])
            flash(t("hosting.forgot_username.sent"), "info")
    return render_template("hosting/auth/forgot_username.html")


@bp.route("/forgot-password", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(5)
def forgot_password():
    signed_in = _redirect_if_signed_in()
    if signed_in:
        return signed_in
    if request.method == "POST":
        from .. import urls

        account = accounts.by_username((request.form.get("username") or "").strip())
        address = (request.form.get("email") or "").strip().lower()
        if (account and not account["deleted_at"] and (account.get("email") or "").lower() == address
                and account.get("email_verified_at") and notifications.configured()):
            token = accounts.issue_password_reset(account)
            notifications.send(account["email"], "reset", username=account["username"],
                               action_url=urls.external_url("auth.reset_password", token=token))
        flash(t("hosting.forgot_password.sent"), "info")
    return render_template("hosting/auth/forgot_password.html")


@bp.route("/reset-password", methods=["GET", "POST"])
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(5)
def reset_password():
    signed_in = _redirect_if_signed_in()
    if signed_in:
        return signed_in
    token = (request.values.get("token") or "").strip()
    if not token:
        flash(t("hosting.reset_password.invalid"), "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        try:
            accounts.check_new_password(request.form.get("new_password") or "",
                                        request.form.get("confirm_new_password") or "")
            done = accounts.consume_password_reset(token, request.form.get("new_password") or "")
        except ServiceError as error:
            flash_error(error)
            return render_template("hosting/auth/reset_password.html", token=token), 400
        if done:
            flash(t("hosting.reset_password.done"), "success")
            return redirect(url_for("auth.login"))
        flash(t("hosting.reset_password.invalid"), "error")
        return redirect(url_for("auth.forgot_password"))
    return render_template("hosting/auth/reset_password.html", token=token)


@bp.get("/verify-email")
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(20)
def verify_email():
    account = accounts.verify_email_token((request.args.get("token") or "").strip())
    if account is None:
        flash(t("hosting.verify.invalid_link"), "error")
        return redirect(url_for("account.verify_email_pending") if auth.current_account() else url_for("auth.login"))
    flash(t("hosting.verify.done"), "success")
    current = auth.current_account()
    if current and current["id"] == account["id"]:
        return redirect(url_for("dashboard.dashboard"))
    return redirect(url_for("auth.login"))


# ── Account states ────────────────────────────────────────────────────────────


@bp.get("/activation-pending")
@auth.exempt(*auth.GATES)
def activation_pending():
    account = auth.current_account()
    if account["approval_status"] == "approved":  # type: ignore[index]
        return redirect(url_for("dashboard.dashboard"))
    if account["approval_status"] == "denied":  # type: ignore[index]
        return redirect(url_for("auth.activation_denied"))
    return render_template("hosting/auth/activation_pending.html")


@bp.get("/activation-denied")
@auth.exempt(*auth.GATES)
def activation_denied():
    account = auth.current_account()
    if account["approval_status"] != "denied":  # type: ignore[index]
        return redirect(url_for("dashboard.dashboard"))
    return render_template("hosting/auth/activation_denied.html",
                           remaining=accounts.denied_remaining(account))  # type: ignore[arg-type]


@bp.get("/pending-deletion")
@auth.exempt(*auth.GATES)
def pending_deletion():
    account = auth.current_account()
    if not account.get("pending_deletion"):  # type: ignore[union-attr]
        return redirect(url_for("dashboard.dashboard"))
    return render_template("hosting/auth/pending_deletion.html", remaining=accounts.deletion_remaining(account),  # type: ignore[arg-type]
                           reason=account.get("pending_deletion_reason") or "")  # type: ignore[union-attr]


@bp.get("/account-suspended")
@auth.public
@auth.exempt(*auth.GATES)
def account_suspended():
    account = accounts.get(session.get(auth.SUSPENDED_KEY))
    if account is None or not accounts.is_suspended(account):
        session.pop(auth.SUSPENDED_KEY, None)
        return redirect(url_for("auth.login"))
    return render_template(
        "hosting/auth/account_suspended.html",
        reason=account["suspend_reason"] if account["suspend_reason_visible"] else "",
        until=account["suspended_until"] if account["suspend_time_visible"] else None,
        permanent=not account["suspended_until"],
    ), 403
