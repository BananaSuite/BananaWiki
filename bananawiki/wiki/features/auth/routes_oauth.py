"""Routes: sign in with the hosting portal, account linking and unlinking.

/platform-oauth/login (``?create=1``: sign up at the portal first, while the
wiki takes hosting accounts only), /platform-oauth/callback, /platform-oauth/connect (the
first sign-in: create an account or connect an existing one),
/platform-oauth/signup (create), /platform-oauth/merge (connect, then the
two-step code), /platform-oauth/link-callback,
/settings/link-platform-account, /settings/unlink-platform-account (from
**Settings → Security → Sign-in methods**), /settings/confirm-with-hosting
and /platform-oauth/confirm-callback (confirming a change, :mod:`.reauth`).
Every route answers 404 while single sign-on is not configured, and while
the wiki takes its own accounts only (:mod:`.signin_modes`).
"""

from __future__ import annotations

import hmac
import secrets
import time
from functools import wraps
from typing import Any

from flask import abort, redirect, render_template, request, session, url_for

from ....core.ratelimit import SqlLimiter
from ....core.web import client_network, safe_next
from ... import accounts, auth, settings
from ...db import db
from ...i18n import t
from ..audit.service import record
from . import invites, reauth, routes_signup, signin, signin_modes, signup, two_step
from . import platform_oauth as oauth
from .blueprint import SIGN_IN_GATES, bp

PENDING_TTL_SECONDS = 900
WINDOW_SECONDS = 600
MAX_PER_IP = 30
# A merge whose password was right waits here for the two-step code (same lifetime as a sign-in's).
MERGE_CODE_KEY = "_platform_oauth_merge_code"


def _configured(view):
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not signin_modes.platform_allowed():
            abort(404)
        if request.method == "POST" or request.endpoint.endswith("callback"):
            if not SqlLimiter(db.session).hit(client_network(), "platform_oauth:ip", MAX_PER_IP, WINDOW_SECONDS):
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


def _finish(user: dict[str, Any], next_url: str, account_id: str, acting: dict[str, Any] | None = None):
    session.pop(oauth.PENDING_KEY, None)
    session.pop(oauth.INVITE_KEY, None)
    return signin.finish(user, remember=False, next_url=next_url, method="platform_oauth",
                         platform_account=account_id, acting=acting)


# Sign in


@bp.get("/platform-oauth/login")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_login():
    if auth.current_user() is not None:
        return redirect("/")
    # "Create an account" on the sign-in page of a wiki that takes hosting accounts only.
    create = request.args.get("create") == "1" and signin_modes.current() == signin_modes.PLATFORM
    return redirect(oauth.authorize_url("login", next_url=safe_next("", request.args.get("next")), create=create))


def _provision(account_id: str, portal_username: str, username: str, invite_code: str | None):
    """Create a wiki account for a portal account, under the sign-up policy."""
    # It never gets a password of its own (password_set 0): it confirms changes with the portal, and may set one.
    user = signup.register(username, None, invite_code=invite_code,
                           password_hash=accounts.hash_password(secrets.token_urlsafe(32)), extra={"password_set": 0})
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
        identity = oauth.fetch_identity(oauth.exchange_code(code, state, "login"))
        account_id, portal_username = identity.sub, identity.username
        refusal = oauth.acting_refusal(identity)
        if refusal:
            raise oauth.OAuthError(refusal)
        user = oauth.linked_user(account_id)
        if user is not None and not oauth.confirm_link(account_id, user):
            user = None  # a stale link (the previous owner of this wiki): the connect page decides
        user = user or oauth.realign_portal_link(account_id, portal_username)
        if user is not None:
            return _finish(user, entry.get("next") or "", account_id, identity.act)
        if identity.act is not None:
            # Accounts are linked and created by the people themselves, never by someone acting as them.
            raise oauth.OAuthError("auth.acting.error.no_account")
        local = accounts.by_username(portal_username)
        # The portal may already know a link of the same-named account (made by 1.4): that settles it.
        # A link to someone else's portal account only makes the name unavailable (see the connect page).
        if local is not None and oauth.link_of(local["id"]) is None \
                and oauth.adopt_portal_link(local, account_id, portal_username):
            return _finish(local, entry.get("next") or "", account_id)
    except oauth.OAuthError as error:
        return _fail(error)
    session.pop(MERGE_CODE_KEY, None)
    pending = {"sub": account_id, "username": portal_username, "next": entry.get("next") or "", "at": time.time()}
    session[oauth.PENDING_KEY] = pending
    if entry.get("create"):
        # The portal account was just created for this wiki: no choice to make, the wiki account follows
        # under the sign-up policy (the connect page takes over when it cannot, e.g. the name is taken).
        return _create_account(pending, portal_username, invites.normalize(session.get(oauth.INVITE_KEY)))
    return redirect(url_for("auth.platform_oauth_connect"))


# The first platform sign-in: create a new wiki account or connect an existing one


def _connect_page(pending: dict[str, Any], *, status: int = 200, create_error: str = "", existing_error: str = "",
                  username: str | None = None, invite: str = "", existing_username: str | None = None):
    """The choice offered on the first platform sign-in, with what applies to this wiki and this name."""
    portal_name = pending["username"]
    local = accounts.by_username(portal_name)
    same_name_free = local is not None and oauth.link_of(local["id"]) is None
    problem = oauth.name_problem(portal_name)
    available = signup.signup_available()
    context = {
        "pending": pending, "signup_available": available, "invite_required": signup.invite_required(),
        "approval_required": settings.approval_required(),
        "name_problem": problem, "same_name_exists": local is not None, "same_name_free": same_name_free,
        "username": username if username is not None else (oauth.suggest_username(portal_name) if available
                                                            else portal_name),
        "invite": invite or invites.normalize(session.get(oauth.INVITE_KEY)), "create_error": create_error, "existing_error": existing_error,
        "existing_username": existing_username if existing_username is not None else (
            local["username"] if same_name_free else ""),
    }
    return render_template("auth/oauth_connect.html", **context), status


@bp.get("/platform-oauth/connect")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_connect():
    pending = _pending()
    if pending is None:
        return _fail(oauth.OAuthError("auth.oauth.error.expired"))
    session.pop(MERGE_CODE_KEY, None)  # back from the code step: the choice starts again
    return _connect_page(pending)


@bp.route("/platform-oauth/signup", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_signup():
    """Create a wiki account for the portal account (name choice, invite code when required)."""
    pending = _pending()
    if pending is None:
        return _fail(oauth.OAuthError("auth.oauth.error.expired"))
    if request.method == "GET":
        return redirect(url_for("auth.platform_oauth_connect"))
    username = (request.form.get("username") or "").strip()[:50]
    return _create_account(pending, username, invites.normalize(request.form.get("invite_code")))


def _create_account(pending: dict[str, Any], username: str, invite: str):
    """Create the wiki account named *username* for the pending portal account, or the connect page saying
    why not (with what was given)."""
    if not signup.signup_available():
        return _connect_page(pending, status=400, create_error=t("auth.signup.error.closed"), username=username)
    # A code tried here counts as one tried on /signup: the same budget bounds guessing on both paths.
    if invite and not SqlLimiter(db.session).hit(client_network(), "signup:ip", routes_signup.MAX_PER_IP,
                                                 routes_signup.WINDOW_SECONDS):
        return _connect_page(pending, status=429, create_error=t("auth.signup.error.too_many"), username=username)
    try:
        if oauth.linked_user(pending["sub"]) is not None:
            raise oauth.OAuthError("auth.oauth.error.account_taken")
        user = _provision(pending["sub"], pending["username"], username, invite)
    except accounts.AccountError as exc:
        message = t(exc.key, **exc.values)
        if exc.key in ("auth.error.username_taken", "auth.error.username_reserved", "auth.error.username_invalid"):
            message = t("auth.oauth.connect.name_unavailable", reason=message, suggestion=oauth.suggest_username(username))
        return _connect_page(pending, status=400, create_error=message, username=username, invite=invite)
    except oauth.OAuthError as error:
        oauth.log.warning("Platform sign-in: creating a wiki account failed: %s", error)
        return _connect_page(pending, status=409 if error.key == "auth.oauth.error.account_taken" else 502,
                             create_error=t(error.key), username=username, invite=invite)
    return _finish(user, pending.get("next") or "", pending["sub"])


def _password_passed(pending: dict[str, Any]) -> dict[str, Any] | None:
    """The wiki account whose password this browser gave in this connect flow, while it owes the two-step code.

    It lapses after a few minutes, and when the password or the two-step
    set-up changed since (the fingerprint of :func:`two_step.fingerprint`).
    """
    state = session.get(MERGE_CODE_KEY)
    if not isinstance(state, dict) or state.get("sub") != pending["sub"]:
        return None
    try:
        started = float(state.get("at", 0))
    except (TypeError, ValueError):
        return None
    local = accounts.by_id(state.get("user_id"))
    if local is None or not 0 <= time.time() - started <= signin.TWO_STEP_PENDING_SECONDS:
        return None
    if not two_step.enabled(local["id"]) or not hmac.compare_digest(str(state.get("fingerprint", "")),
                                                                   two_step.fingerprint(local)):
        return None
    return local


def _current_link(pending: dict[str, Any], local: dict[str, Any]) -> dict[str, Any] | None:
    """*local*'s link here; one to another portal account that the portal no longer has is dropped first
    (the new owner of a wiki connecting its administrator before the sign-out job ran)."""
    current = oauth.link_of(local["id"])
    if current is not None and current["account_id"] != pending["sub"] \
            and not oauth.confirm_link(current["account_id"], local):
        return None
    return current


def _link_existing(pending: dict[str, Any], local: dict[str, Any]) -> None:
    """Connect the portal account to *local*, whose credentials were just proven, or raise OAuthError."""
    current = _current_link(pending, local)
    if current is not None and current["account_id"] != pending["sub"]:
        raise oauth.OAuthError("auth.oauth.error.wiki_account_linked")
    if current is not None:
        return  # already connected to this portal account (another tab finished first)
    if oauth.linked_user(pending["sub"]) is not None:
        raise oauth.OAuthError("auth.oauth.error.account_taken")
    oauth.link(local, pending["sub"], pending["username"])


def _merge_code_step(pending: dict[str, Any], local: dict[str, Any]):
    """The second step of connecting an existing account: link only once its two-step code is accepted.

    The code is counted like a sign-in's (:func:`signin.check_code`), and the
    sign-in that follows does not ask for it again.
    """
    if request.method == "POST":
        try:
            method = signin.check_code(local, request.form.get("code"))
            _link_existing(pending, local)
        except signin.Refused as refusal:
            auth.flash_t(refusal.key, "error", **refusal.values)
            return render_template("auth/oauth_merge.html", pending=pending, local=local,
                                   code_step=True), refusal.status
        except oauth.OAuthError as error:
            session.pop(MERGE_CODE_KEY, None)
            oauth.log.warning("Platform sign-in: connecting %s failed: %s", local["username"], error)
            return _connect_page(pending, status=409, existing_error=t(error.key),
                                 existing_username=local["username"])
        session.pop(MERGE_CODE_KEY, None)
        session.pop(oauth.PENDING_KEY, None)
        response = signin.finish_second_step(local, {"remember": False, "next": pending.get("next") or "",
                                                     "method": "platform_oauth", "default": "/",
                                                     "platform_account": pending["sub"]}, method)
        auth.flash_t("auth.oauth.linked", "success")
        return response
    return render_template("auth/oauth_merge.html", pending=pending, local=local, code_step=True)


@bp.route("/platform-oauth/merge", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
@_configured
def platform_oauth_merge():
    """Connect an account that already exists on this wiki: its user name and password, then its code.

    The account may have any name (not only the portal's): someone known here
    as "alexbanana" connects that account even when "alex" belongs to someone
    else. An account with two-step verification also answers a code before
    the link is made: the password alone must not attach a portal account to it.
    """
    pending = _pending()
    if pending is None:
        return _fail(oauth.OAuthError("auth.oauth.error.expired"))
    local = _password_passed(pending)
    if local is not None:
        return _merge_code_step(pending, local)
    session.pop(MERGE_CODE_KEY, None)
    if request.method == "GET":
        return redirect(url_for("auth.platform_oauth_connect"))
    # 1.6 forms sent only the password: they were about the account named like the portal account.
    username = (request.form.get("username") or pending["username"]).strip()[:50]
    try:
        user = signin.verify(username, request.form.get("password") or "")
    except signin.Refused as refusal:
        return _connect_page(pending, status=refusal.status, existing_username=username,
                             existing_error=t(refusal.key, **refusal.values))
    try:
        if two_step.enabled(user["id"]):
            current = _current_link(pending, user)
            if current is not None and current["account_id"] != pending["sub"]:
                raise oauth.OAuthError("auth.oauth.error.wiki_account_linked")
            session[MERGE_CODE_KEY] = {"sub": pending["sub"], "user_id": user["id"], "at": time.time(),
                                       "fingerprint": two_step.fingerprint(user)}
            return redirect(url_for("auth.platform_oauth_merge"))
        _link_existing(pending, user)
    except oauth.OAuthError as error:
        oauth.log.warning("Platform sign-in: connecting %s failed: %s", user["username"], error)
        return _connect_page(pending, status=409, existing_error=t(error.key), existing_username=username)
    auth.flash_t("auth.oauth.linked", "success")
    return _finish(user, pending.get("next") or "", pending["sub"])


# Sign-in methods in the account settings: connecting, disconnecting, confirming a change

METHODS_PAGE = "users.security"


def _methods_page():
    return redirect(url_for(METHODS_PAGE) + "#sign-in-methods")


def _own_password(user: dict[str, Any]) -> str | None:
    """Why the account's own confirmation (password, or a recent confirmation with the portal) fails (a
    translation key with its values), or None when it passed; refused on someone's behalf."""
    from ..users.service import ProfileError, password_ok

    try:
        return None if password_ok(user, request.form.get("password")) else "auth.error.current_password_wrong"
    except ProfileError as error:
        return error.key


def disconnect_refusal(user: dict[str, Any]) -> str | None:
    """Why *user* may not disconnect the portal (a translation key), or None: an account without a password, or
    a wiki that takes hosting accounts only, would have no way back in."""
    if not reauth.has_password(user):
        return "auth.methods.disconnect.needs_password"
    if not signin_modes.password_allowed():
        return "auth.methods.disconnect.platform_only"
    return None


@bp.route("/settings/link-platform-account", methods=["GET", "POST"])
@_configured
def platform_oauth_link_account():
    """Connect the signed-in account to a hosting account: its password (proof the session's holder knows
    it), then the portal's sign-in. The old page of its own now lives in Sign-in methods."""
    user = auth.current_user()
    if request.method == "GET":
        return _methods_page()
    if auth.on_behalf():
        auth.flash_t("auth.acting.error.refused", "error")
        return _methods_page()
    if oauth.link_of(user["id"]) is not None:
        auth.flash_t("auth.oauth.already_linked", "info")
        return _methods_page()
    problem = _own_password(user)
    if problem:
        auth.flash_t(problem, "error")
        return _methods_page()
    return redirect(oauth.authorize_url("link", user_id=user["id"], bound={"sid": auth.current_session_id()}))


@bp.get("/platform-oauth/link-callback")
@_configured
def platform_oauth_link_callback():
    user = auth.current_user()
    state = request.args.get("state", "")
    try:
        entry = oauth.consume_state(state, "link")
        if entry.get("uid") != user["id"] or entry.get("sid", auth.current_session_id()) != auth.current_session_id():
            raise oauth.OAuthError("auth.oauth.error.expired")
        if auth.on_behalf():
            raise oauth.OAuthError("auth.acting.error.refused")
        if request.args.get("error"):
            raise oauth.OAuthError("auth.oauth.error.cancelled")
        identity = oauth.fetch_identity(oauth.exchange_code(request.args.get("code", ""), state, "link"))
        if identity.act is not None:
            raise oauth.OAuthError("auth.acting.error.no_account")
        if oauth.linked_user(identity.sub) is not None:
            raise oauth.OAuthError("auth.methods.connect.taken")
        oauth.link(user, identity.sub, identity.username)
    except oauth.OAuthError as error:
        return _fail(error, METHODS_PAGE)
    record("user.platform_linked", target_type="user", target_id=user["id"],
           details={"username": user["username"], "via": "settings"})
    auth.flash_t("auth.oauth.linked", "success")
    return _methods_page()


@bp.post("/settings/unlink-platform-account")
@_configured
def platform_oauth_unlink_account():
    user = auth.current_user()
    if auth.on_behalf():
        auth.flash_t("auth.acting.error.refused", "error")
        return _methods_page()
    problem = disconnect_refusal(user) or _own_password(user)
    if problem:
        auth.flash_t(problem, "error")
        return _methods_page()
    try:
        oauth.unlink(user)
    except oauth.OAuthError as error:
        return _fail(error, METHODS_PAGE)
    record("user.platform_unlinked", target_type="user", target_id=user["id"],
           details={"username": user["username"], "via": "settings"})
    auth.flash_t("auth.oauth.unlinked", "success")
    return _methods_page()


@bp.post("/settings/confirm-with-hosting")
@auth.exempt("account_steps")
@_configured
def platform_oauth_confirm():
    """Start confirming a sensitive change with the portal (see :mod:`.reauth`); ``confirm_next`` is the page
    to come back to."""
    user = auth.current_user()
    back = safe_next(url_for(METHODS_PAGE), request.form.get("confirm_next"))
    if not reauth.hosting_available(user):
        auth.flash_t("auth.acting.error.refused" if auth.on_behalf() else "auth.reverify.error.not_linked", "error")
        return redirect(back)
    return redirect(oauth.authorize_url("confirm", next_url=back, user_id=user["id"], fresh=True,
                                        bound={"sid": auth.current_session_id(), "started": time.time()}))


@bp.get("/platform-oauth/confirm-callback")
@auth.exempt("account_steps")
@_configured
def platform_oauth_confirm_callback():
    user = auth.current_user()
    state = request.args.get("state", "")
    back = url_for(METHODS_PAGE)
    try:
        entry = oauth.consume_state(state, "confirm")
        back = safe_next(back, entry.get("next"))
        if entry.get("uid") != user["id"] or entry.get("sid") != auth.current_session_id():
            raise oauth.OAuthError("auth.oauth.error.expired")
        if request.args.get("error"):
            raise oauth.OAuthError("auth.oauth.error.cancelled")
        identity = oauth.fetch_identity(oauth.exchange_code(request.args.get("code", ""), state, "confirm"))
        problem = reauth.accepted(entry, identity, user)
        if problem:
            raise oauth.OAuthError(problem)
    except oauth.OAuthError as error:
        oauth.log.warning("Confirming with the portal failed: %s", error)
        auth.flash_t(error.key, "error")
        return redirect(back)
    auth.mark_reverified()
    record("user.reverified", target_type="user", target_id=user["id"],
           details={"username": user["username"], "via": "hosting"})
    auth.flash_t("auth.reverify.done", "success", minutes=auth.REVERIFY_SECONDS // 60)
    return redirect(back)
