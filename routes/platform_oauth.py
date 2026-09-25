"""OAuth 2.0 consumer (client) for the hosting platform SSO.

When the hosting platform has ``platform_oauth_enabled`` turned on, each wiki
instance receives environment variables that configure it as an OAuth client
of the hosting portal.  This module registers the wiki-side routes that handle
the SSO login flow, account linking, and account merging.

**Environment variables** (injected by the hosting platform's instance manager):

- ``BW_PLATFORM_OAUTH_ENABLED``: ``"1"`` when the feature is active.
- ``BW_PLATFORM_OAUTH_CLIENT_ID``: this instance's OAuth client ID.
- ``BW_PLATFORM_OAUTH_CLIENT_SECRET``: this instance's OAuth client secret.
- ``BW_PLATFORM_OAUTH_PORTAL_BASE``: base URL of the hosting portal.
- ``BW_PLATFORM_OAUTH_AUTHORIZE_URL``: full authorize endpoint URL.
- ``BW_PLATFORM_OAUTH_TOKEN_URL``: full token endpoint URL.
- ``BW_PLATFORM_OAUTH_USERINFO_URL``: full userinfo endpoint URL.
- ``BW_PLATFORM_OAUTH_LINK_URL``: full link-creation endpoint URL.
- ``BW_PLATFORM_OAUTH_UNLINK_URL``: full unlink endpoint URL.
- ``BW_PLATFORM_OAUTH_LINK_STATUS_URL``: full link-status endpoint URL.
- ``BW_PLATFORM_INSTANCE_ID``: this instance's ID in the hosting DB.

**Account merging logic**

When a user signs in via platform OAuth for the first time:

1. **No local wiki user with the same username** → a new wiki user is created
   and linked to the hosting account.  The user is role ``"user"`` (not admin)
   regardless of their hosting-account role.

2. **Local wiki user exists with the same username AND is already linked to
   the same hosting account** → the user is logged in directly (normal case
   after initial linking).

3. **Local wiki user exists with the same username but is linked to a
   DIFFERENT hosting account** → conflict.  The OAuth login is blocked with a
   message to contact the admin, because the local username is claimed by
   someone else's hosting account.

4. **Local wiki user exists with the same username and is NOT linked to ANY
   hosting account** → the user is asked to prove ownership by entering the
   local wiki password.  On success, the accounts are linked.  On failure,
   they are given the option to log in with the local password instead.

5. **Existing wiki user wants to link their hosting account later** → they can
   visit ``/settings/link-platform-account`` and go through the OAuth flow
   while logged in.  This also requires entering their current password as a
   safeguard.
"""

from http_transport import open_http

import json
import os
import secrets
import time
import urllib.request
import urllib.parse
import logging
from datetime import datetime, timezone

from flask import (
    abort, flash, redirect, render_template,
    request, session, url_for,
)

import db
from helpers import (
    get_current_user, login_required, rate_limit,
    get_safe_next_url,
)
from helpers._passwords import check_password_hash, generate_password_hash
from wiki_logger import log_action

logger = logging.getLogger("platform_oauth")

def _is_platform_oauth_enabled():
    """Return ``True`` when the wiki is configured for platform OAuth SSO."""
    return os.environ.get("BW_PLATFORM_OAUTH_ENABLED") == "1"


def _oauth_config():
    """Return a dict of all OAuth config values, or ``None`` if disabled."""
    if not _is_platform_oauth_enabled():
        return None
    return {
        "client_id": os.environ.get("BW_PLATFORM_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("BW_PLATFORM_OAUTH_CLIENT_SECRET", ""),
        "portal_base": os.environ.get("BW_PLATFORM_OAUTH_PORTAL_BASE", ""),
        "authorize_url": os.environ.get("BW_PLATFORM_OAUTH_AUTHORIZE_URL", ""),
        "token_url": os.environ.get("BW_PLATFORM_OAUTH_TOKEN_URL", ""),
        "userinfo_url": os.environ.get("BW_PLATFORM_OAUTH_USERINFO_URL", ""),
        "link_url": os.environ.get("BW_PLATFORM_OAUTH_LINK_URL", ""),
        "unlink_url": os.environ.get("BW_PLATFORM_OAUTH_UNLINK_URL", ""),
        "link_status_url": os.environ.get("BW_PLATFORM_OAUTH_LINK_STATUS_URL", ""),
        "instance_id": os.environ.get("BW_PLATFORM_INSTANCE_ID", ""),
    }


def _http_post_json(url, data, timeout=10):
    """POST JSON data to *url* and return the parsed JSON response.

    Returns ``(data_dict, error_string)``.
    """
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with open_http(req, timeout=timeout, public_only=False) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            return err_body, err_body.get("error", str(e))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


def _http_get_json(url, bearer_token=None, timeout=10):
    """GET *url* with an optional Bearer token and return parsed JSON.

    Returns ``(data_dict, error_string)``.
    """
    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with open_http(req, timeout=timeout, public_only=False) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            return err_body, err_body.get("error", str(e))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


# State tokens are what stops a third party from replaying a callback at us,
# so they have to survive the round trip to the hosting portal: the key is a
# random token and the value a dict of redirect info, both kept in the
# session rather than in a local store.


def _generate_state_token(redirect_after=None):
    """Generate a state token and store it in the session."""
    token = secrets.token_hex(24)
    if "platform_oauth_states" not in session:
        session["platform_oauth_states"] = {}
    session["platform_oauth_states"][token] = {
        "redirect_after": redirect_after or url_for("home"),
        "created_at": time.time(),
    }
    # Prune stale state tokens
    now = time.time()
    stale = [k for k, v in session["platform_oauth_states"].items()
             if now - v.get("created_at", 0) > 600]
    for k in stale:
        session["platform_oauth_states"].pop(k, None)
    session.modified = True
    return token


def _consume_state_token(token):
    """Validate and consume a state token.  Returns the redirect URL or None."""
    if not token or "platform_oauth_states" not in session:
        return None
    state_data = session["platform_oauth_states"].pop(token, None)
    session.modified = True
    if state_data is None:
        return None
    created = state_data.get("created_at", 0)
    if time.time() - created > 600:
        return None
    return state_data.get("redirect_after", url_for("home"))


# Access tokens live in the session and nowhere else.  They are deliberately
# never written to the wiki database, so a database leak cannot be replayed
# against the hosting portal.


def _store_oauth_token(access_token):
    """Store the OAuth access token in the user's session."""
    session["platform_oauth_access_token"] = access_token
    session["platform_oauth_token_expires_at"] = time.time() + 3600
    session.modified = True


def _get_oauth_token():
    """Return the stored OAuth access token, or None if expired/missing."""
    token = session.get("platform_oauth_access_token")
    expires = session.get("platform_oauth_token_expires_at", 0)
    if token and time.time() < expires:
        return token
    return None


def _clear_oauth_token():
    """Remove the OAuth token from the session."""
    session.pop("platform_oauth_access_token", None)
    session.pop("platform_oauth_token_expires_at", None)
    session.modified = True


def _link_status(instance_id, wiki_user_id):
    """Check whether *wiki_user_id* is linked to a hosting account."""
    cfg = _oauth_config()
    if not cfg:
        return {"linked": False}
    url = f"{cfg['link_status_url']}?instance_id={urllib.parse.quote(instance_id)}&wiki_user_id={urllib.parse.quote(wiki_user_id)}"
    data, error = _http_get_json(url)
    if error:
        logger.warning("Failed to check OAuth link status: %s", error)
        return {"linked": False}
    return data if isinstance(data, dict) else {"linked": False}


def _create_link(instance_id, account_id, wiki_user_id, wiki_username):
    """Create a link between a hosting account and a wiki user."""
    cfg = _oauth_config()
    if not cfg:
        return False, "OAuth is not configured on this instance."
    client_secret = cfg.get("client_secret", "")
    if not client_secret:
        return False, "OAuth client secret is not configured."
    data, error = _http_post_json(cfg["link_url"], {
        "instance_id": instance_id,
        "account_id": account_id,
        "wiki_user_id": wiki_user_id,
        "wiki_username": wiki_username,
        "client_secret": client_secret,
    })
    if error:
        return False, error
    return data.get("ok", False), data.get("error")


def _delete_link(instance_id, account_id):
    """Remove the link between a hosting account and a wiki user."""
    cfg = _oauth_config()
    if not cfg:
        return False, "OAuth is not configured on this instance."
    client_secret = cfg.get("client_secret", "")
    if not client_secret:
        return False, "OAuth client secret is not configured."
    data, error = _http_post_json(cfg["unlink_url"], {
        "instance_id": instance_id,
        "account_id": account_id,
        "client_secret": client_secret,
    })
    if error:
        return False, error
    return data.get("ok", False), data.get("error")


def _fetch_userinfo(access_token):
    """Fetch user info from the hosting portal using *access_token*.

    Returns ``(userinfo_dict, error_string)``.
    """
    cfg = _oauth_config()
    if not cfg:
        return None, "OAuth is not configured on this instance."
    return _http_get_json(cfg["userinfo_url"], bearer_token=access_token)


def register_platform_oauth_routes(app):
    """Register platform OAuth SSO routes on the Flask app.

    These routes are only active when ``BW_PLATFORM_OAUTH_ENABLED=1``.
    """

    @app.route("/platform-oauth/login")
    @rate_limit(10, 60)
    def platform_oauth_login():
        """Redirect the user to the hosting portal's OAuth authorize page.

        The wiki's redirect_uri points back to the callback route below.
        """
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        if get_current_user():
            return redirect(url_for("home"))

        redirect_after = get_safe_next_url(request.args.get("next"))
        state = _generate_state_token(redirect_after)

        # Build the authorise URL
        callback_url = url_for("platform_oauth_callback", _external=True)
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": cfg["client_id"],
            "redirect_uri": callback_url,
            "state": state,
        })
        return redirect(f"{cfg['authorize_url']}?{params}")

    @app.route("/platform-oauth/callback")
    @rate_limit(10, 60)
    def platform_oauth_callback():
        """Handle the OAuth authorisation code callback from the hosting portal.

        Exchanges the code for an access token, fetches user info, and
        logs the user in (creating/linking accounts as needed).
        """
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        error = request.args.get("error", "").strip()

        if error:
            flash(
                "Platform login was cancelled or denied by the hosting portal.",
                "error",
            )
            return redirect(url_for("login"))

        if not code:
            flash("Missing authorisation code from the hosting portal.", "error")
            return redirect(url_for("login"))

        redirect_after = _consume_state_token(state)
        if redirect_after is None:
            flash(
                "Your login session has expired. Please try again.",
                "error",
            )
            return redirect(url_for("login"))

        # Exchange code for access token
        token_data, token_error = _exchange_code_for_token(cfg, code)
        if token_error or not token_data:
            flash(f"Failed to authenticate with the hosting platform: {token_error}", "error")
            return redirect(url_for("login"))

        access_token = token_data.get("access_token")
        if not access_token:
            flash("Failed to obtain an access token from the hosting platform.", "error")
            return redirect(url_for("login"))

        # Fetch user info
        userinfo, ui_error = _fetch_userinfo(access_token)
        if ui_error or not userinfo:
            flash(f"Failed to fetch user info: {ui_error}", "error")
            return redirect(url_for("login"))

        hosting_account_id = userinfo.get("sub")
        hosting_username = userinfo.get("username", "")
        if not hosting_account_id or not hosting_username:
            flash("Invalid user info received from the hosting platform.", "error")
            return redirect(url_for("login"))

        # Attempt to log in or create/link the wiki account
        result = _process_oauth_login(
            cfg=cfg,
            hosting_account_id=hosting_account_id,
            hosting_username=hosting_username,
        )

        if result.get("error"):
            flash(result["error"], "error")
            return redirect(url_for("login"))

        if result.get("needs_password_verification"):
            # Username collision: a local user with the same username exists
            # but is not linked.  Show the merge-verification page.
            session["oauth_merge_data"] = {
                "hosting_account_id": hosting_account_id,
                "hosting_username": hosting_username,
                "access_token": access_token,
            }
            return render_template(
                "auth/platform_oauth_merge.html",
                hosting_username=hosting_username,
                local_username=result["local_username"],
                state_token=state,
            )

        # Success: log in
        wiki_user = result.get("user")
        if wiki_user:
            _complete_login(wiki_user, access_token)
            flash(f"Signed in as {wiki_user['username']}.", "success")
            return redirect(redirect_after)

        flash("Failed to complete platform login.", "error")
        return redirect(url_for("login"))

    # Only reached when the hosting account's username already exists
    # locally; the password check is what proves the same person owns both
    # accounts before they are joined.
    @app.route("/platform-oauth/merge", methods=["POST"])
    @rate_limit(10, 60)
    def platform_oauth_merge():
        """Handle the merge verification form submission.

        The user has a local wiki account with the same username as their
        hosting account.  They must enter the local wiki password to confirm
        they own both accounts.
        """
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        merge_data = session.pop("oauth_merge_data", None)
        if not merge_data:
            flash("Your merge session has expired. Please try again.", "error")
            return redirect(url_for("login"))

        password = request.form.get("password", "")
        local_user = db.get_user_by_username(merge_data["hosting_username"])

        if not local_user or not check_password_hash(local_user["password"], password):
            flash(
                "Incorrect password. You can try again, or log in with your "
                "local wiki password instead.",
                "error",
            )
            return redirect(url_for("login"))

        # Password verified: create the link and log in
        ok, link_error = _create_link(
            cfg["instance_id"],
            merge_data["hosting_account_id"],
            local_user["id"],
            local_user["username"],
        )
        if not ok:
            flash(f"Failed to link accounts: {link_error}", "error")
            return redirect(url_for("login"))

        _complete_login(local_user, merge_data["access_token"])
        flash(
            f"Signed in as {local_user['username']}. Your wiki account has been "
            f"linked to your platform account.",
            "success",
        )
        return redirect(url_for("home"))

    @app.route("/settings/link-platform-account", methods=["GET", "POST"])
    @login_required
    @rate_limit(5, 60)
    def platform_oauth_link_account():
        """Link the currently logged-in wiki user to their hosting account.

        On GET: shows a form asking for the user's current password.
        On POST: verifies the password, then checks if this hosting
        account is already linked to a different wiki user.  If not,
        creates the link.
        """
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        current_user = get_current_user()
        if not current_user:
            return redirect(url_for("login"))

        if request.method == "GET":
            # Check if already linked
            status = _link_status(cfg["instance_id"], current_user["id"])
            already_linked = status.get("linked", False)
            linked_username = status.get("hosting_username", "")
            return render_template(
                "auth/link_platform_account.html",
                already_linked=already_linked,
                linked_username=linked_username,
                current_user=current_user,
            )

        # POST
        password = request.form.get("password", "")
        if not check_password_hash(current_user["password"], password):
            flash("Incorrect password.", "error")
            return redirect(url_for("platform_oauth_link_account"))

        # Check if already linked
        status = _link_status(cfg["instance_id"], current_user["id"])
        if status.get("linked"):
            flash("Your wiki account is already linked to a hosting account.", "info")
            return redirect(url_for("user_settings"))

        # Initiate OAuth flow with a special state noting we are linking
        state = _generate_state_token(url_for("user_settings"))
        callback_url = url_for("platform_oauth_link_callback", _external=True)
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": cfg["client_id"],
            "redirect_uri": callback_url,
            "state": state,
        })
        session["platform_oauth_linking_user_id"] = current_user["id"]
        session.modified = True
        return redirect(f"{cfg['authorize_url']}?{params}")

    @app.route("/platform-oauth/link-callback")
    @rate_limit(10, 60)
    def platform_oauth_link_callback():
        """Handle the OAuth callback when linking an existing wiki account."""
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code:
            flash("Missing authorisation code.", "error")
            return redirect(url_for("user_settings"))

        redirect_after = _consume_state_token(state) or url_for("user_settings")

        linking_user_id = session.pop("platform_oauth_linking_user_id", None)
        if not linking_user_id:
            flash("Your link session has expired. Please try again.", "error")
            return redirect(url_for("user_settings"))

        wiki_user = db.get_user_by_id(linking_user_id)
        if not wiki_user:
            flash("User not found.", "error")
            return redirect(url_for("user_settings"))

        # Exchange code for token
        token_data, token_error = _exchange_code_for_token(cfg, code)
        if token_error:
            flash(f"Authentication failed: {token_error}", "error")
            return redirect(url_for("user_settings"))

        access_token = token_data.get("access_token")
        if not access_token:
            flash("Failed to obtain access token.", "error")
            return redirect(url_for("user_settings"))

        userinfo, ui_error = _fetch_userinfo(access_token)
        if ui_error:
            flash(f"Failed to fetch user info: {ui_error}", "error")
            return redirect(url_for("user_settings"))

        hosting_account_id = userinfo.get("sub")
        if not hosting_account_id:
            flash("Invalid user info.", "error")
            return redirect(url_for("user_settings"))

        # Check if this hosting account is already linked to a different wiki user
        existing_status = _link_status(cfg["instance_id"], wiki_user["id"])
        if existing_status.get("linked"):
            flash("This wiki account is already linked to a hosting account.", "info")
            return redirect(redirect_after)

        # Create the link
        ok, link_error = _create_link(
            cfg["instance_id"],
            hosting_account_id,
            wiki_user["id"],
            wiki_user["username"],
        )
        if not ok:
            if "already linked" in (link_error or "").lower():
                flash(
                    "This hosting account is already linked to a different wiki user "
                    "on this instance. Contact an administrator to resolve this.",
                    "error",
                )
            else:
                flash(f"Failed to link accounts: {link_error}", "error")
            return redirect(redirect_after)

        flash(
            f"Your wiki account is now linked to the hosting account "
            f"'{userinfo.get('username', '')}'.",
            "success",
        )
        return redirect(redirect_after)

    @app.route("/settings/unlink-platform-account", methods=["POST"])
    @login_required
    @rate_limit(5, 60)
    def platform_oauth_unlink_account():
        """Unlink the current wiki user from their hosting account."""
        cfg = _oauth_config()
        if not cfg:
            abort(404)

        current_user = get_current_user()
        if not current_user:
            return redirect(url_for("login"))

        password = request.form.get("password", "")
        if not check_password_hash(current_user["password"], password):
            flash("Incorrect password.", "error")
            return redirect(url_for("user_settings"))

        status = _link_status(cfg["instance_id"], current_user["id"])
        if not status.get("linked"):
            flash("Your wiki account is not linked to any hosting account.", "info")
            return redirect(url_for("user_settings"))

        ok, error = _delete_link(
            cfg["instance_id"],
            status.get("hosting_account_id", ""),
        )
        if not ok:
            flash(f"Failed to unlink accounts: {error}", "error")
        else:
            flash("Your wiki account has been unlinked from the hosting account.", "success")
        return redirect(url_for("user_settings"))

    @app.context_processor
    def inject_platform_oauth():
        """Make ``platform_oauth_enabled`` available to all templates."""
        return {
            "platform_oauth_enabled": _is_platform_oauth_enabled(),
        }


def _exchange_code_for_token(cfg, code):
    """Exchange an authorisation code for an access token.

    Returns ``(token_data_dict, error_string)``.
    """
    callback_url = url_for("platform_oauth_callback", _external=True)
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": callback_url,
    }).encode("utf-8")

    req = urllib.request.Request(
        cfg["token_url"],
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with open_http(req, timeout=10, public_only=False) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
            return token_data, None
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            return err_body, err_body.get("error_description", err_body.get("error", str(e)))
        except Exception:
            return None, str(e)
    except Exception as e:
        return None, str(e)


def _process_oauth_login(cfg, hosting_account_id, hosting_username):
    """Process an OAuth login attempt.

    Handles account creation, linking, and collision detection.

    Returns a dict:
      - ``user``: the wiki user row on success.
      - ``error``: error message on failure.
      - ``needs_password_verification``: ``True`` when a merge is needed.
      - ``local_username``: the conflicting local username when merge needed.
    """
    # Check if this hosting account is already linked to a wiki user
    # by scanning all links for this instance + account.
    # We do this by checking each wiki user against the hosting account.
    # Instead, we use the hosting portal's link-status endpoint for each
    # candidate or we search locally.

    # First, try to find the link on the hosting side.
    # Since we don't have the wiki_user_id yet, we search all wiki users
    # that might match.  Actually, the simplest approach: check if there's
    # a local wiki user with the same username.
    local_user = db.get_user_by_username(hosting_username)

    if local_user:
        # A local user with this username exists.
        # Check if they are already linked to the OAuth account.
        status = _link_status(cfg["instance_id"], local_user["id"])
        if status.get("linked"):
            linked_account_id = status.get("hosting_account_id")
            if linked_account_id == hosting_account_id:
                # Same user: log them in
                return {"user": local_user}
            else:
                # The local user is linked to a DIFFERENT hosting account
                return {
                    "error": (
                        f"The username '{hosting_username}' is already linked to a "
                        f"different hosting account on this wiki. Please contact "
                        f"an administrator to resolve this conflict."
                    ),
                }
        else:
            # Local user exists but is NOT linked to any hosting account.
            # This is a potential merge candidate: require password verification.
            return {
                "needs_password_verification": True,
                "local_username": local_user["username"],
            }

    # No local user with this username: create a new account and link it.
    random_password = secrets.token_urlsafe(24)
    hashed_pw = generate_password_hash(random_password)
    try:
        new_user_id = db.create_user(hosting_username, hashed_pw, role="user")
    except Exception as exc:
        logger.exception("Failed to create wiki user via OAuth")
        return {"error": f"Failed to create wiki account: {exc}"}

    # Re-fetch the created user
    new_user = db.get_user_by_id(new_user_id)
    if not new_user:
        return {"error": "Failed to create wiki account."}

    # Create the link on the hosting side
    ok, link_error = _create_link(
        cfg["instance_id"],
        hosting_account_id,
        new_user["id"],
        hosting_username,
    )
    if not ok:
        # Roll back the user creation
        try:
            db.delete_user(new_user_id)
        except Exception:
            logger.exception("Failed to roll back user creation after link failure")
        return {"error": f"Failed to link accounts: {link_error}"}

    # Log the creation
    log_action(
        "oauth_account_created",
        request,
        username=hosting_username,
        hosting_account_id=hosting_account_id,
    )

    return {"user": new_user}


def _complete_login(wiki_user, access_token):
    """Finalise the login for a wiki user authenticated via OAuth.

    Sets the session, stores the OAuth token, and records the login.
    """
    session.clear()
    session.permanent = True
    session["user_id"] = wiki_user["id"]
    session["user_role"] = wiki_user["role"]
    from helpers._auth_sessions import establish_user_session
    establish_user_session(wiki_user["id"], auth_method="platform_oauth")
    _store_oauth_token(access_token)

    # Record the login
    try:
        db.update_user(
            wiki_user["id"],
            last_login_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        logger.exception("Failed to update last_login_at for OAuth-authenticated user")
