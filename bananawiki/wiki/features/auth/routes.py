"""Routes: first-run setup, sign-in and sign-out, account status and forced steps.

/setup, /login, /admin/sign-in (administrator sign-in, also during maintenance;
POST /admin for 1.4 forms),
/logout, /session-conflict[/force], /app-selector, /account-status (and the
1.4 status URLs), /account-suspended/delete, /maintenance, /lockdown,
/force-change-password, /impersonation/stop.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any

from flask import abort, current_app, redirect, render_template, request, session, url_for

from ....core import passwords
from ....core.timeutil import now_sql, parse
from ....core.web import safe_next
from ... import accounts, auth, settings
from ...db import db
from ...i18n import t
from ...permissions import ADMIN_ROLES
from ...registry import emit
from ...templating import nav_items
from . import signin, signup
from .blueprint import ALL_GATES, SIGN_IN_GATES, bp


def _setup_token_ok() -> bool:
    """The installation token, accepted only from a POSTed form (never from the URL)."""
    expected = current_app.config["BW"].setup_token
    marker = hashlib.sha256(expected.encode()).hexdigest()
    if hmac.compare_digest(str(session.get("_setup_authorized", "")), marker):
        return True
    supplied = request.form.get("setup_token", "") if request.method == "POST" else ""
    if supplied and hmac.compare_digest(supplied.encode(), expected.encode()):
        session["_setup_authorized"] = marker
        return True
    return False


@bp.route("/setup", methods=["GET", "POST"])
@auth.public
@auth.exempt(*ALL_GATES)
def setup():
    """Create the first administrator. Requires the installation token."""
    if settings.setup_done():
        return redirect(url_for("auth.login"))
    if not _setup_token_ok():
        if request.method == "POST":
            auth.flash_t("auth.setup.bad_token", "error")
        return render_template("auth/setup_token.html"), 403 if request.method == "POST" else 200
    error = None
    if request.method == "POST" and request.form.get("step") == "create":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        language = request.form.get("language") or "en"
        if password != request.form.get("confirm_password", ""):
            error = t("auth.error.passwords_differ")
        else:
            try:
                with db.transaction():
                    if settings.setup_done() or db.scalar("SELECT 1 FROM users LIMIT 1"):
                        abort(409)
                    user = accounts.create(username, password, role="owner", emit_event=False,
                                           extra={"onboarding_required": 1, "is_superuser": 1})
                    settings.update({"interface_language": language if language in ("en", "it") else "en"})
                    settings.update({"setup_done": 1}, internal=True)
            except accounts.AccountError as exc:
                error = t(exc.key, **exc.values)
            else:
                session.pop("_setup_authorized", None)
                emit("user.created", user=user)
                auth.start_session(user, method="setup")
                return redirect(url_for("auth.onboarding"))
    return render_template("auth/setup.html", error=error, form=request.form)


# ── Sign-in ───────────────────────────────────────────────────────────────────


def _credentials() -> tuple[str, str, bool, str]:
    username = (request.form.get("username") or "").strip()[:50]
    password = request.form.get("password") or ""
    remember = request.form.get("remember_me") in ("1", "on", "true")
    next_url = request.values.get("next", "")
    return username, password, remember, next_url


def _sign_in(template: str, *, admin_only: bool = False, default: str = "/", **context: Any):
    """Handle a POSTed sign-in form rendered by *template*."""
    username, password, remember, next_url = _credentials()
    try:
        user = signin.verify(username, password, admin_only=admin_only)
    except signin.Refused as refusal:
        auth.flash_t(refusal.key, "error")
        return render_template(template, next_url=next_url, username=username, **context), refusal.status
    return signin.finish(user, remember=remember, next_url=next_url, default=default)


@bp.route("/login", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def login():
    if auth.current_user() is not None and request.method == "GET":
        return redirect(safe_next("/", request.args.get("next")))
    context = {"signup_available": signup.signup_available()}
    if request.method == "GET":
        return render_template("auth/login.html", next_url=request.args.get("next", ""), username="", **context)
    return _sign_in("auth/login.html", **context)


def _admin_home() -> str:
    return url_for("admin.dashboard") if "admin.dashboard" in current_app.view_functions else "/"


@bp.route("/admin/sign-in", methods=["GET", "POST"])
@bp.post("/admin")  # 1.4 form target; GET /admin belongs to the admin area
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def admin_login():
    """Administrator sign-in; the way back in while the wiki is under maintenance."""
    user = auth.current_user()
    if request.method == "GET":
        if user is not None and auth.is_admin(user):
            return redirect(_admin_home())
        return render_template("auth/admin_login.html", next_url=request.args.get("next", ""), username="")
    return _sign_in("auth/admin_login.html", admin_only=True, default=_admin_home())


@bp.route("/logout", methods=["GET", "POST"])
@auth.public
@auth.exempt(*ALL_GATES)
def logout():
    if request.method == "GET":
        # A GET cannot carry a CSRF token; show a confirmation instead of acting.
        if auth.current_user() is None:
            return redirect(url_for("auth.login"))
        return render_template("auth/logout.html")
    auth.end_session()
    auth.flash_t("auth.signed_out", "success")
    return redirect(url_for("auth.login"))


@bp.get("/session-conflict")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def session_conflict():
    """Shown to the browser whose session ended because the account signed in elsewhere."""
    if auth.current_user() is not None:
        return redirect("/")
    user = accounts.by_id(session.get(auth.SESSION_CONFLICT_KEY))
    if user is None:
        session.pop(auth.SESSION_CONFLICT_KEY, None)
        return redirect(url_for("auth.login", next=request.args.get("next") or None))
    return render_template("auth/session_conflict.html", username=user["username"],
                           next_url=request.args.get("next", ""))


@bp.post("/session-conflict/force")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def session_conflict_force():
    """Sign in here anyway: the new session ends the one on the other device."""
    return _sign_in("auth/session_conflict.html")


@bp.get("/app-selector")
def app_selector():
    """After sign-in (``login_app_selector``): choose between the wiki and the enabled apps."""
    apps = nav_items("apps")
    if not apps:
        return redirect("/")
    return render_template("auth/app_selector.html", apps=apps)


# ── Account status ────────────────────────────────────────────────────────────


def _suspended_deletion_allowed(user: dict[str, Any]) -> bool:
    return bool(settings.get("suspended_account_deletion_enabled")) and not user.get("is_superuser") \
        and not auth.is_impersonating()


def _denied_deletion_time(user: dict[str, Any]) -> datetime | None:
    denied_at = parse(user.get("denied_at"))
    hours = int(settings.get("approval_denied_timeout_hours", 24) or 0)
    if denied_at is None or hours <= 0:
        return None
    return denied_at + timedelta(hours=hours)


@bp.route("/account-status")
@auth.exempt("approval", "account_steps", "maintenance")
def account_status():
    user = auth.current_user()
    state = auth.account_block(user) if user else None
    if state is None:
        return redirect("/")
    if state == "denied" and not user.get("denied_notified") and not auth.is_impersonating():
        db.execute("UPDATE users SET denied_notified = 1 WHERE id = ?", (user["id"],))
    return render_template(
        "auth/account_status.html", state=state, user=user,
        can_delete=state == "suspended" and _suspended_deletion_allowed(user),
        deletion_time=_denied_deletion_time(user) if state == "denied" else None,
    )


@bp.route("/activation-pending")
@bp.route("/activation-denied")
@bp.route("/account-suspended")
@auth.exempt("approval", "account_steps", "maintenance")
def legacy_status_urls():
    return redirect(url_for("auth.account_status"))


def _other_active_admins(user_id: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM users WHERE role IN ('admin', 'owner') AND suspended = 0 AND id != ?",
        (user_id,), default=0,
    ))


@bp.post("/account-suspended/delete")
@auth.exempt("approval", "account_steps", "maintenance")
def suspended_delete():
    """A suspended user deletes their own account (``suspended_account_deletion_enabled``)."""
    user = auth.current_user()
    if auth.account_block(user) != "suspended":
        return redirect("/")
    if not _suspended_deletion_allowed(user):
        abort(403)
    password = request.form.get("password") or ""
    if len(password) > passwords.MAX_LENGTH or not passwords.verify_password(user["password"], password):
        auth.flash_t("auth.error.current_password_wrong", "error")
        return redirect(url_for("auth.account_status"))
    if user["role"] in ADMIN_ROLES and _other_active_admins(user["id"]) == 0:
        auth.flash_t("auth.status.delete.last_admin", "error")
        return redirect(url_for("auth.account_status"))
    try:
        accounts.delete(user, deleted_by=user["id"])
    except accounts.AccountError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return redirect(url_for("auth.account_status"))
    auth.end_session()
    auth.flash_t("auth.status.delete.done", "info")
    return redirect(url_for("auth.login"))


@bp.route("/maintenance")
@auth.public
@auth.exempt(*ALL_GATES)
def maintenance():
    if not settings.maintenance_active():
        return redirect("/")
    return render_template("auth/maintenance.html", message=settings.get("maintenance_message") or "")


@bp.route("/lockdown")
@auth.public
@auth.exempt(*ALL_GATES)
def lockdown_legacy():
    return redirect(url_for("auth.maintenance"))


# ── Forced steps ──────────────────────────────────────────────────────────────


@bp.route("/force-change-password", methods=["GET", "POST"])
@auth.exempt("account_steps")
def force_password_change():
    user = auth.current_user()
    if not user.get("force_password_change"):
        return redirect("/")
    error = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        new_username = (request.form.get("username") or "").strip()
        from ..users.service import ProfileError, password_ok

        try:
            current_ok = password_ok(user, current)
        except ProfileError as exc:
            current_ok, error = False, t(exc.key)
        if not current_ok:
            error = error or t("auth.error.current_password_wrong")
        elif new != request.form.get("confirm_password", ""):
            error = t("auth.error.passwords_differ")
        elif passwords.verify_password(user["password"], new):
            error = t("auth.error.password_unchanged")
        else:
            try:
                if new_username and new_username != user["username"]:
                    accounts.rename(user, new_username, changed_by=user["id"])
                accounts.set_password(user["id"], new, keep_session_id=auth.current_session_id())
            except accounts.AccountError as exc:
                error = t(exc.key, **exc.values)
            else:
                auth.flash_t("auth.password_changed", "success")
                auth.refresh_current_user()
                return redirect(signin.landing_url(auth.current_user(), None))
    return render_template("auth/force_password_change.html", error=error, user=user)


@bp.post("/impersonation/stop")
@auth.exempt(*ALL_GATES)
def stop_impersonation():
    if not auth.is_impersonating():
        return redirect("/")
    target = auth.current_user()
    auth.stop_impersonation()
    db.execute(
        "UPDATE impersonation_logs SET ended_at = ? WHERE admin_id = ? AND target_user_id = ? AND ended_at IS NULL",
        (now_sql(), auth.real_user()["id"], target["id"]),
    )
    auth.flash_t("auth.impersonation_ended", "success")
    return redirect("/")
