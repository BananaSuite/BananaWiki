"""Routes: sign in with the hosting portal, account linking and unlinking.

/platform-oauth/login, /platform-oauth/callback, /platform-oauth/merge,
/platform-oauth/signup, /platform-oauth/link-callback,
/settings/link-platform-account, /settings/unlink-platform-account.
Every route answers 404 while single sign-on is not configured.
"""

from __future__ import annotations

import secrets
import time
from functools import wraps
from typing import Any

from flask import abort, redirect, render_template, request, session, url_for

from ....core.ratelimit import SqlLimiter
from ....core.web import allow_form_action, client_ip, safe_next
from ... import accounts, auth, settings
from ...db import db
from ...i18n import t
from . import invites, signin, signup
from . import platform_oauth as oauth
from .blueprint import SIGN_IN_GATES, bp

PENDING_TTL_SECONDS = 900
WINDOW_SECONDS = 600
MAX_PER_IP = 30


def _configured(view):
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if oauth.config() is None:
            abort(404)
        if request.method == "POST" or request.endpoint.endswith("callback"):
            if not SqlLimiter(db.session).hit(client_ip(), "platform_oauth:ip", MAX_PER_IP, WINDOW_SECONDS):
                abort(429)
        return view(*args, **kwargs)

    return wrapper


def _fail(error: oauth.OAuthError, endpoint: str = "auth.login"):
    oauth.log.warning("Platform sign-in failed: %s", error)
    auth.flash_t(error.key, "error")
    return redirect(url_for(endpoint))


def _pending() -> dict[str, Any] | None:
    pending = session.get(oauth.PENDING_KEY)
    if not pending or time.time() - pending.get("at", 0) > PENDING_TTL_SECONDS:
        session.pop(oauth.PENDING_KEY, None)
        return None
    return pending


def _finish(user: dict[str, Any], next_url: str):
    session.pop(oauth.PENDING_KEY, None)
    return signin.finish(user, remember=False, next_url=next_url, method="platform_oauth")


# ── Sign in ───────────────────────────────────────────────────────────────────


@bp.get("/platform-oauth/login")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_login():
    if auth.current_user() is not None:
        return redirect("/")
    return redirect(oauth.authorize_url("login", next_url=safe_next("", request.args.get("next"))))


def _can_provision_directly(username: str) -> bool:
    try:
        accounts.validate_username(username)
    except accounts.AccountError:
        return False
    return not signup.invite_required() and not accounts.username_taken(username)


def _provision(account_id: str, portal_username: str, username: str, invite_code: str | None):
    """Create a wiki account for a portal account, under the sign-up policy."""
    user = signup.register(username, None, invite_code=invite_code,
                           password_hash=accounts.hash_password(secrets.token_urlsafe(32)))
    try:
        oauth.link(user, account_id, portal_username)
    except oauth.OAuthError:
        accounts.delete(user)
        raise
    return user


@bp.get("/platform-oauth/callback")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_callback():
    state = request.args.get("state", "")
    try:
        entry = oauth.consume_state(state, "login")
        if request.args.get("error"):
            raise oauth.OAuthError("auth.oauth.error.cancelled")
        code = request.args.get("code", "")
        if not code:
            raise oauth.OAuthError("auth.oauth.error.portal", "callback without a code")
        account_id, portal_username = oauth.fetch_profile(oauth.exchange_code(code, state, "login"))
        user = oauth.linked_user(account_id) or oauth.realign_portal_link(account_id, portal_username)
        if user is not None:
            return _finish(user, entry.get("next") or "")
        local = accounts.by_username(portal_username)
        if local is not None and oauth.link_of(local["id"]) is None:
            adopted = oauth.adopt_portal_link(local, account_id, portal_username)
            if adopted:
                return _finish(local, entry.get("next") or "")
            if adopted is False:
                raise oauth.OAuthError("auth.oauth.error.username_linked")
        session[oauth.PENDING_KEY] = {"sub": account_id, "username": portal_username,
                                      "next": entry.get("next") or "", "at": time.time()}
        if local is not None:
            return redirect(url_for("auth.platform_oauth_merge"))
        if _can_provision_directly(portal_username):
            return _finish(_provision(account_id, portal_username, portal_username, None), entry.get("next") or "")
        return redirect(url_for("auth.platform_oauth_signup"))
    except oauth.OAuthError as error:
        return _fail(error)
    except accounts.AccountError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return redirect(url_for("auth.platform_oauth_signup"))


@bp.route("/platform-oauth/merge", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_merge():
    """The portal username exists here: prove ownership with the wiki password to link them."""
    pending = _pending()
    if pending is None:
        return _fail(oauth.OAuthError("auth.oauth.error.expired"))
    local = accounts.by_username(pending["username"])
    if local is None or oauth.link_of(local["id"]) is not None:
        return redirect(url_for("auth.platform_oauth_signup"))
    if request.method == "POST":
        try:
            user = signin.verify(local["username"], request.form.get("password") or "")
            oauth.link(user, pending["sub"], pending["username"])
        except signin.Refused as refusal:
            auth.flash_t(refusal.key, "error")
            return render_template("auth/oauth_merge.html", pending=pending, local=local), refusal.status
        except oauth.OAuthError as error:
            return _fail(error)
        auth.flash_t("auth.oauth.linked", "success")
        return _finish(user, pending.get("next") or "")
    return render_template("auth/oauth_merge.html", pending=pending, local=local)


@bp.route("/platform-oauth/signup", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_signup():
    """Create a wiki account for the portal account (username choice, invite code when required)."""
    pending = _pending()
    if pending is None:
        return _fail(oauth.OAuthError("auth.oauth.error.expired"))
    if not signup.signup_available():
        return _fail(oauth.OAuthError("auth.signup.error.closed"))
    context = {"pending": pending, "invite_required": signup.invite_required(),
               "approval_required": settings.approval_required()}
    if request.method == "GET":
        return render_template("auth/oauth_signup.html", username=pending["username"], invite="", **context)
    username = (request.form.get("username") or "").strip()[:50]
    invite = invites.normalize(request.form.get("invite_code"))
    try:
        user = _provision(pending["sub"], pending["username"], username, invite)
    except (accounts.AccountError, oauth.OAuthError) as exc:
        message = t(exc.key, **getattr(exc, "values", {}))
        return render_template("auth/oauth_signup.html", username=username, invite=invite, error=message,
                               **context), 400
    return _finish(user, pending.get("next") or "")


# ── Linking from the account settings ─────────────────────────────────────────


@bp.route("/settings/link-platform-account", methods=["GET", "POST"])
@_configured
def platform_oauth_link_account():
    user = auth.current_user()
    if auth.is_impersonating():
        abort(403)
    link = oauth.link_of(user["id"])
    if link is None:
        # The link form below is answered with a redirect to the portal.
        allow_form_action(oauth.config()["authorize_url"])
    if request.method == "GET":
        return render_template("auth/oauth_link.html", link=link)
    if link is not None:
        auth.flash_t("auth.oauth.already_linked", "info")
        return redirect(url_for("auth.platform_oauth_link_account"))
    try:
        signin.verify(user["username"], request.form.get("password") or "")
    except signin.Refused as refusal:
        auth.flash_t(refusal.key, "error")
        return render_template("auth/oauth_link.html", link=None), refusal.status
    return redirect(oauth.authorize_url("link", user_id=user["id"]))


@bp.get("/platform-oauth/link-callback")
@_configured
def platform_oauth_link_callback():
    user = auth.current_user()
    state = request.args.get("state", "")
    try:
        entry = oauth.consume_state(state, "link")
        if entry.get("uid") != user["id"] or auth.is_impersonating():
            raise oauth.OAuthError("auth.oauth.error.expired")
        if request.args.get("error"):
            raise oauth.OAuthError("auth.oauth.error.cancelled")
        account_id, portal_username = oauth.fetch_profile(
            oauth.exchange_code(request.args.get("code", ""), state, "link"))
        oauth.link(user, account_id, portal_username)
    except oauth.OAuthError as error:
        return _fail(error, "auth.platform_oauth_link_account")
    auth.flash_t("auth.oauth.linked", "success")
    return redirect(url_for("auth.platform_oauth_link_account"))


@bp.post("/settings/unlink-platform-account")
@_configured
def platform_oauth_unlink_account():
    user = auth.current_user()
    if auth.is_impersonating():
        abort(403)
    try:
        signin.verify(user["username"], request.form.get("password") or "")
        oauth.unlink(user)
    except signin.Refused as refusal:
        auth.flash_t(refusal.key, "error")
        return redirect(url_for("auth.platform_oauth_link_account"))
    except oauth.OAuthError as error:
        return _fail(error, "auth.platform_oauth_link_account")
    auth.flash_t("auth.oauth.unlinked", "success")
    return redirect(url_for("auth.platform_oauth_link_account"))
