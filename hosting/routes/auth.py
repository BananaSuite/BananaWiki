"""Authentication routes for the hosting portal."""

import hashlib
import hmac
import re
import time
import json
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
    flash,
    abort,
    Response,
)

# The hosting portal reuses the main wiki's built-in interface languages
# (English + Italian) but is intentionally restricted to those two: hosting
# users can only switch between en and it, with no support for the custom
# language packs that the main wiki admin can install.
from helpers._interface_languages import BUILTIN_INTERFACE_LANGUAGES
from helpers._passwords import check_password_hash, generate_password_hash
from helpers._validation import get_safe_next_url
from helpers._translations import t as translate

from ..db import (
    get_account_by_id,
    get_account_by_username,
    get_account_by_email,
    issue_email_verification,
    clear_email_verification,
    verify_email_token,
    delete_account,
    get_hosting_db_context,
    get_instances_for_account,
    record_hosting_login_attempt,
    count_recent_hosting_login_attempts,
    clear_hosting_login_attempts,
    check_and_record_hosting_rate_limit,
    count_admin_accounts,
    create_signup_account,
    get_hosting_settings,
    is_account_suspended,
    check_account_suspension_expired,
    get_signup_mode,
    get_hosting_activation_required,
    get_hosting_activation_denied_timeout_seconds,
    get_hosting_activation_denied_deletion_enabled,
    get_approval_notify_email,
    get_approval_notify_mode,
    update_hosting_account,
    clear_email_flag,
    create_hosting_session,
    get_hosting_session,
    get_hosting_session_id,
    list_active_hosting_sessions,
    list_hosting_session_history,
    clear_hosting_session_history,
    touch_hosting_session,
    revoke_hosting_session,
    revoke_hosting_session_token,
    revoke_all_hosting_sessions,
    change_hosting_password_preserving_session,
    get_hosting_api_enabled,
    list_api_tokens,
    api_scopes_for_account,
    MAX_ACTIVE_API_TOKENS,
    API_TOKEN_NAME_MAX_LENGTH,
)
from ..instance_manager import terminate_instance
from .. import config as hosting_config
from ..instance_paths import _format_host_for_url
from ..email_delivery import (
    send_email,
    render_email_html,
    is_configured as email_is_configured,
)
from ..notifications import notify_admin_signup_pending
from helpers._session_metadata import (
    describe_session_user_agent,
    normalize_session_ip,
    normalize_session_user_agent,
)


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,30}$")
_PASSWORD_COMPLEXITY_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)")
_DUMMY_HASH = generate_password_hash("dummy-constant-time-guard")

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds

_PASSWORD_MAX_LENGTH = 1000
_TERMS_VERSION = "2026-07-22"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Bot protection constants (mirroring helpers/_bot_protection.py)
_BOT_MIN_FORM_SECONDS = 1.5
_BOT_MAX_FORM_SECONDS = 4 * 3600
_HOSTING_AUTH_SESSION_KEY = "hosting_auth_session_token"


def _establish_hosting_session(account, auth_method="password"):
    """Create and attach a persistent hosting login session."""
    _row_id, raw_token = create_hosting_session(
        account["id"],
        int(account.get("session_version") or 0),
        ip_address=normalize_session_ip(request.remote_addr),
        user_agent=normalize_session_user_agent(request.headers.get("User-Agent")),
        auth_method=auth_method,
    )
    session[_HOSTING_AUTH_SESSION_KEY] = raw_token


def _validate_hosting_session():
    """Validate a managed hosting session and reject tracked legacy cookies."""
    raw_token = session.get(_HOSTING_AUTH_SESSION_KEY)
    if not raw_token:
        if session.get("hosting_restricted_suspension_session"):
            return True
        # Real pre-migration hosting cookies carry a session version. Force a
        # one-time re-login so they cannot bypass per-session revocation.
        return current_app.testing and "hosting_session_version" not in session
    row = get_hosting_session(raw_token)
    real_account_id = (
        session.get("hosting_impersonator_account_id")
        or session.get("hosting_account_id")
    )
    real_account = get_account_by_id(real_account_id) if real_account_id else None
    if (
        row is None
        or real_account is None
        or row["account_id"] != real_account_id
        or int(row["session_version"]) != int(real_account.get("session_version") or 0)
    ):
        session.clear()
        return False
    try:
        last_seen = datetime.fromisoformat(row["last_seen_at"])
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        touch_due = (datetime.now(timezone.utc) - last_seen).total_seconds() >= 300
    except (TypeError, ValueError):
        touch_due = True
    if touch_due:
        touch_hosting_session(
            raw_token,
            ip_address=normalize_session_ip(request.remote_addr),
        )
    return True


def _has_letter_and_number(password):
    """Return ``True`` when *password* contains at least one letter and digit."""
    return bool(_PASSWORD_COMPLEXITY_RE.search(password))


def _email_verification_required(settings=None):
    settings = settings or get_hosting_settings()
    if not bool(settings and settings.get("email_verification_required", 0)):
        return False
    # Don't enforce verification when email delivery is unavailable
    # (provider not configured or daily quota exhausted): let users
    # continue and verify once delivery is restored.
    if not email_is_configured():
        return False
    from ..email_delivery import get_daily_email_stats
    sent, limit = get_daily_email_stats()
    if limit > 0 and sent >= limit:
        return False
    return True


def _email_policy_active():
    """Return whether production email gates should affect this request."""
    return not current_app.config.get("TESTING", False)


def _public_portal_url(endpoint, **values):
    """Build links from configured public origin, never the request Host.

    Email links must not trust a caller-controlled Host header.  In domain
    mode the portal has a canonical hostname; port-mode deployments use the
    explicitly configured public host and scheme.
    """
    relative = url_for(endpoint, _external=False, **values)
    if hosting_config.HOSTING_MODE == "subdomain":
        host = hosting_config.EFFECTIVE_PORTAL_DOMAIN or hosting_config.BASE_DOMAIN
        scheme = "https"
    else:
        host = _format_host_for_url(hosting_config.HOSTING_PUBLIC_HOST)
        scheme = hosting_config.HOSTING_PUBLIC_SCHEME
        if hosting_config.HOSTING_PORT not in (80, 443):
            host = f"{host}:{hosting_config.HOSTING_PORT}"
    return f"{scheme}://{host}{relative}"


def _redirect_to_static_site(path):
    """Redirect to the public static site (apex domain), never the portal.

    Legal documents (Terms, Privacy, compliance) live on the static site
    only; the portal just points users there.
    """
    if hosting_config.BASE_DOMAIN:
        return redirect("https://{}{}".format(hosting_config.BASE_DOMAIN, path))
    # Fallback (port mode without a base domain): go to the portal origin.
    return redirect("/" + path.lstrip("/"))


def _verification_cooldown_remaining(account, settings=None):
    settings = settings or get_hosting_settings()
    cooldown = max(30, int(settings.get("email_verification_cooldown_seconds", 60))) if settings else 60
    sent_at = account.get("email_verification_sent_at") if account else None
    if not sent_at:
        return 0
    try:
        sent = datetime.fromisoformat(sent_at)
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        return max(0, int(cooldown - (datetime.now(timezone.utc) - sent).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _send_verification_email(account, *, ignore_cooldown=False):
    """Issue and send a fresh verification link. Returns ``(ok, message)``."""
    if not account or not account.get("email"):
        return False, "Add an email address first."
    settings = get_hosting_settings()
    remaining = _verification_cooldown_remaining(account, settings)
    if remaining and not ignore_cooldown:
        return False, f"Please wait {remaining} seconds before requesting another email."
    if not email_is_configured():
        return False, "Email delivery is not configured. Contact the platform administrator."

    raw_token = secrets.token_urlsafe(32)
    expires = (
        datetime.now(timezone.utc)
        + timedelta(seconds=hosting_config.HOSTING_EMAIL_TOKEN_TTL_SECONDS)
    ).isoformat()
    issue_email_verification(account["id"], account["email"], raw_token, expires)
    verify_url = _public_portal_url("hosting_verify_email", token=raw_token)
    username = account.get("username") or "there"
    ok, error = send_email(
        to=account["email"],
        subject="Verify your BananaWiki email",
        text=(
            f"Hello {username},\n\n"
            f"Verify your BananaWiki Hosting email by opening:\n{verify_url}\n\n"
            "The link expires in 24 hours. If you did not request this, ignore it."
        ),
        html=render_email_html(
            title="Confirm your email address",
            eyebrow="EMAIL VERIFICATION",
            text=(
                f"Hello {username},\n\n"
                "Confirm this email address to finish securing your BananaWiki Hosting account.\n\n"
                "This link expires in 24 hours. If you did not request it, you can safely ignore this email."
            ),
            action_url=verify_url,
            action_label="Verify email address",
        ),
    )
    if not ok:
        clear_email_verification(account["id"])
        return False, f"The verification email could not be sent: {error}."
    return True, "Verification email sent. Check your inbox and spam folder."


# Bot protection helpers (self-contained; no import from main wiki helpers)

def _hosting_get_secret() -> bytes:
    key = current_app.secret_key
    if isinstance(key, str):
        return key.encode("utf-8")
    return bytes(key)


def _hosting_sign(timestamp: str) -> str:
    return hmac.new(_hosting_get_secret(), timestamp.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _hosting_is_testing() -> bool:
    try:
        if current_app.config.get("TESTING"):
            return True
        if not current_app.config.get("WTF_CSRF_ENABLED", True):
            return True
    except RuntimeError:
        pass
    return False


def hosting_generate_form_token() -> str:
    """Return a signed timestamp token to embed in a hosting form."""
    ts = str(int(time.time()))
    return f"{ts}.{_hosting_sign(ts)}"


def hosting_check_bot_protection(request) -> tuple[bool, str]:
    """Return ``(blocked, reason)`` for bot-protection validation."""
    if _hosting_is_testing():
        return False, ""

    honeypot = request.form.get("website", "")
    if honeypot:
        return True, "honeypot"

    raw_token = request.form.get("_form_time", "")
    if not raw_token:
        return True, "missing_token"

    try:
        ts_str, sig = raw_token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return True, "invalid_token"

    expected = _hosting_sign(ts_str)
    if not hmac.compare_digest(sig, expected):
        return True, "invalid_token"

    elapsed = time.time() - ts
    if elapsed < _BOT_MIN_FORM_SECONDS:
        return True, "too_fast"
    if elapsed > _BOT_MAX_FORM_SECONDS:
        return True, "expired_token"

    return False, ""


def _hosting_bot_protection_enabled() -> bool:
    """Return ``True`` when hosting bot protection is active."""
    try:
        settings = get_hosting_settings()
        if settings is None:
            return True
        return bool(settings.get("bot_protection_enabled", 1))
    except Exception:
        return True


def _redirect_to_login_with_next():
    """Redirect to the hosting login page, preserving the originally
    requested URL via ``?next=`` so the user lands back on it after a
    successful login.

    Only attaches ``next`` for safe ``GET`` requests to non-root paths.
    POSTs and other mutations are not round-tripped (re-submitting after
    login is not safe), and ``/`` is omitted to avoid a permanent
    ``?next=/`` on the login URL.
    """
    if request.method == "GET":
        next_url = request.path
        if request.query_string:
            next_url += "?" + request.query_string.decode("utf-8")
        if next_url and next_url != "/":
            return redirect(url_for("hosting_login", next=next_url))
    return redirect(url_for("hosting_login"))


def _redirect_if_logged_in():
    """Redirect visitors who are already authenticated away from the public
    auth pages (login, signup, account recovery) back to the dashboard.

    Returns ``None`` when the visitor is anonymous so the route can keep
    rendering normally.  Restricted suspension sessions count as
    authenticated: the dashboard will bounce them to the suspension page.
    """
    if session.get("hosting_account_id") and _validate_hosting_session():
        return redirect(url_for("hosting_dashboard"))
    return None


def _flash_suspension_message(account):
    """Build and flash a detailed suspension message for the user.

    Mirrors the main BananaWiki pattern: always states the account is
    suspended, then conditionally appends the reason and/or remaining
    time based on the visibility flags set by the admin.
    """
    parts = ["Your account has been suspended."]
    reason = account["suspend_reason"]
    reason_visible = account["suspend_reason_visible"]
    suspended_until = account["suspended_until"]
    time_visible = account["suspend_time_visible"]

    if reason and reason_visible:
        parts.append(f"Reason: {reason}")
    if suspended_until and time_visible:
        try:
            from datetime import datetime as _dt, timezone as _tz
            exp = _dt.fromisoformat(suspended_until)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            expiry_str = exp.strftime("%Y-%m-%d %H:%M UTC")
            parts.append(f"Suspended until {expiry_str}.")
        except (ValueError, TypeError):
            pass
    elif not suspended_until:
        parts.append("This suspension is permanent.")
    parts.append("Please contact the platform administrator for help.")
    flash(" ".join(parts), "error")


def hosting_login_required(f):
    """Decorator that requires a logged-in hosting portal session."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "hosting_account_id" not in session:
            return _redirect_to_login_with_next()
        if not _validate_hosting_session():
            flash("Your session expired or was terminated. Log in again.", "info")
            return _redirect_to_login_with_next()
        account = get_account_by_id(session["hosting_account_id"])
        if account is None:
            session.pop("hosting_account_id", None)
            return _redirect_to_login_with_next()
        if "hosting_impersonator_account_id" not in session:
            current_version = int(account.get("session_version") or 0)
            cookie_version = session.get("hosting_session_version")
            if cookie_version is None:
                session["hosting_session_version"] = current_version
            elif int(cookie_version) != current_version:
                session.clear()
                flash("Your session was invalidated by a security change. Log in again.", "info")
                return _redirect_to_login_with_next()
        # Auto-unsuspend if timed suspension has expired
        if account["suspended"] and check_account_suspension_expired(account["id"]):
            account = get_account_by_id(account["id"])
        if is_account_suspended(account["id"]):
            return redirect(url_for("hosting_account_suspended"))
        endpoint = request.endpoint or ""
        # Pending-deletion gate: user can only see the countdown page + logout
        if account.get("pending_deletion"):
            pd_allowed = {
                "hosting_pending_deletion", "hosting_logout",
                "hosting_change_language", "hosting_set_theme",
                "hosting_terms", "hosting_privacy", "static",
            }
            if endpoint not in pd_allowed:
                return redirect(url_for("hosting_pending_deletion"))
        # Admin-flagged invalid email: force user to re-enter a valid
        # email before they can do anything else.
        email_flag_allowed = {
            "hosting_email_flagged", "hosting_submit_flagged_email",
            "hosting_logout", "hosting_change_language",
            "hosting_set_theme", "hosting_terms", "hosting_privacy", "static",
        }
        if account.get("email_flagged_invalid") and endpoint not in email_flag_allowed:
            return redirect(url_for("hosting_email_flagged"))
        email_allowed = {
            "hosting_account", "hosting_verify_email", "hosting_resend_verification",
            "hosting_email_verification_pending", "hosting_set_contact_email",
            "hosting_logout", "hosting_change_language", "hosting_set_theme",
            "hosting_terms", "hosting_privacy", "hosting_revoke_api_token", "static",
        }
        settings = get_hosting_settings()
        if (_email_policy_active() and _email_verification_required(settings) and account.get("email")
                and not account.get("email_verified_at") and endpoint not in email_allowed):
            return redirect(url_for("hosting_email_verification_pending"))
        # Pending accounts can only see the pending page
        approval_status = account.get("approval_status", "approved")
        if approval_status in ("pending", "denied"):
            allowed = {
                "hosting_activation_pending", "hosting_activation_denied",
                "hosting_logout", "hosting_change_language",
                "hosting_set_theme", "hosting_account", "hosting_verify_email",
                "hosting_resend_verification", "hosting_email_verification_pending",
                "hosting_set_contact_email", "hosting_terms", "hosting_privacy", "static",
            }
            if endpoint not in allowed:
                if approval_status == "pending":
                    return redirect(url_for("hosting_activation_pending"))
                else:
                    return redirect(url_for("hosting_activation_denied"))
        return f(*args, **kwargs)
    return wrapper


def hosting_admin_required(f):
    """Decorator that requires a logged-in hosting portal admin."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "hosting_account_id" not in session:
            return _redirect_to_login_with_next()
        if not _validate_hosting_session():
            flash("Your session expired or was terminated. Log in again.", "info")
            return _redirect_to_login_with_next()
        account = get_account_by_id(session["hosting_account_id"])
        if account is None:
            session.pop("hosting_account_id", None)
            return _redirect_to_login_with_next()
        if "hosting_impersonator_account_id" not in session:
            current_version = int(account.get("session_version") or 0)
            cookie_version = session.get("hosting_session_version")
            if cookie_version is None:
                session["hosting_session_version"] = current_version
            elif int(cookie_version) != current_version:
                session.clear()
                flash("Your session was invalidated by a security change. Log in again.", "info")
                return _redirect_to_login_with_next()
        # Auto-unsuspend if timed suspension has expired
        if account["suspended"] and check_account_suspension_expired(account["id"]):
            account = get_account_by_id(account["id"])
        if is_account_suspended(account["id"]):
            return redirect(url_for("hosting_account_suspended"))
        endpoint = request.endpoint or ""
        # Pending-deletion gate (same as hosting_login_required)
        if account.get("pending_deletion"):
            pd_allowed = {
                "hosting_pending_deletion", "hosting_logout",
                "hosting_change_language", "hosting_set_theme",
                "hosting_terms", "hosting_privacy", "static",
            }
            if endpoint not in pd_allowed:
                return redirect(url_for("hosting_pending_deletion"))
        # Admin-flagged invalid email: force re-entry even for admins
        email_flag_allowed = {
            "hosting_email_flagged", "hosting_submit_flagged_email",
            "hosting_logout", "hosting_change_language",
            "hosting_set_theme", "hosting_terms", "hosting_privacy", "static",
        }
        if account.get("email_flagged_invalid") and endpoint not in email_flag_allowed:
            return redirect(url_for("hosting_email_flagged"))
        email_allowed = {
            "hosting_account", "hosting_verify_email", "hosting_resend_verification",
            "hosting_email_verification_pending", "hosting_logout",
            "hosting_set_contact_email", "hosting_change_language",
            "hosting_set_theme", "hosting_terms",
            "hosting_privacy", "static",
        }
        settings = get_hosting_settings()
        if (settings and settings.get("email_required", 1)
                and settings.get("ask_email_existing_users", 1)
                and not account.get("email")
                and request.method != "GET"
                and not current_app.config.get("TESTING", False)
                and endpoint not in {
                    "hosting_set_contact_email", "hosting_logout",
                    "hosting_change_language", "hosting_set_theme",
                }):
            flash("Add your required contact email before continuing.", "info")
            return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))
        if (_email_policy_active() and _email_verification_required(settings) and account.get("email")
                and not account.get("email_verified_at") and endpoint not in email_allowed):
            return redirect(url_for("hosting_email_verification_pending"))
        if not account["is_admin"]:
            flash("You do not have admin access.", "error")
            return redirect(url_for("hosting_dashboard"))
        # Even admins get blocked if their own account is pending or denied
        approval_status = account.get("approval_status", "approved")
        if approval_status in ("pending", "denied"):
            allowed = {
                "hosting_activation_pending", "hosting_activation_denied",
                "hosting_logout", "hosting_change_language",
                "hosting_set_theme", "hosting_account", "hosting_verify_email",
                "hosting_resend_verification", "hosting_email_verification_pending",
                "hosting_terms", "hosting_privacy", "static",
            }
            if endpoint not in allowed:
                if approval_status == "pending":
                    return redirect(url_for("hosting_activation_pending"))
                else:
                    return redirect(url_for("hosting_activation_denied"))
        return f(*args, **kwargs)
    return wrapper


def hosting_rate_limit(max_requests=20, window=60):
    """Route decorator that enforces a per-IP rate limit on hosting endpoints.

    Only POST (mutation) requests consume quota.  GET requests pass through
    without being counted so that simply visiting a page does not lock out
    the form submission.
    """
    def decorator(f):
        bucket = f"hosting_{f.__name__}"
        @wraps(f)
        def wrapper(*args, **kwargs):
            if request.method != "GET":
                ip = request.remote_addr or "unknown"
                if not check_and_record_hosting_rate_limit(ip, bucket, max_requests, window):
                    flash("Too many requests. Please slow down.", "error")
                    abort(429)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def get_current_account():
    """Return the current hosting portal account row, or ``None``."""
    aid = session.get("hosting_account_id")
    if not aid:
        return None
    if session.get(_HOSTING_AUTH_SESSION_KEY) and not _validate_hosting_session():
        return None
    return get_account_by_id(aid)


def get_hosting_impersonator_account():
    """Return the real admin account behind an impersonated hosting session."""
    aid = session.get("hosting_impersonator_account_id")
    if not aid:
        return None
    return get_account_by_id(aid)


def _safe_local_referrer():
    """Return ``request.referrer`` only when it points back to this host.

    Used by routes that want to round-trip back to the page the user came
    from (e.g. the language switcher) without becoming an open-redirect
    vector.  Returns ``None`` if the referrer is missing, malformed, or
    targets a different host.
    """
    ref = request.referrer or ""
    if not ref:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(ref)
    except Exception:
        return None
    # Only allow same-origin referrers.  ``request.host`` includes the port
    # when non-default, which matches how Flask builds ``url_for`` URLs.
    if parsed.netloc and parsed.netloc != request.host:
        return None
    return ref




def _complete_hosting_login(account, next_url=None, auth_method="password"):
    """Establish a login after all required factors and apply account gates."""
    if is_account_suspended(account["id"]):
        # Create a session so the dedicated suspension page can
        # look up the account and display details.
        session.clear()
        session.permanent = True
        session["hosting_account_id"] = account["id"]
        session["hosting_session_version"] = int(account.get("session_version") or 0)
        session["hosting_restricted_suspension_session"] = True
        return redirect(url_for("hosting_account_suspended"))

    clear_hosting_login_attempts(request.remote_addr or "unknown")
    session.clear()
    session.permanent = True
    session["hosting_account_id"] = account["id"]
    session["hosting_session_version"] = int(account.get("session_version") or 0)
    _establish_hosting_session(account, auth_method=auth_method)

    # Pending-deletion: let the user in but send them straight to
    # the countdown page.  The hosting_login_required decorator
    # will also enforce this gate on subsequent requests.
    if account.get("pending_deletion"):
        return redirect(url_for("hosting_pending_deletion"))

    approval_status = account.get("approval_status", "approved")
    if approval_status == "pending":
        flash("Your account is awaiting approval from an administrator.", "info")
        return redirect(url_for("hosting_activation_pending"))
    if approval_status == "denied":
        return redirect(url_for("hosting_activation_denied"))

    next_url = get_safe_next_url(next_url)

    settings = get_hosting_settings()
    if (_email_policy_active() and settings and settings.get("ask_email_existing_users")
            and not account.get("email")
            and not account.get("email_prompt_dismissed")):
        return redirect(url_for("hosting_account"))
    if (_email_policy_active() and _email_verification_required(settings) and account.get("email")
            and not account.get("email_verified_at")):
        return redirect(url_for("hosting_email_verification_pending"))
    return redirect(next_url or url_for("hosting_dashboard"))


# Session key for the nonce that makes each API token form single use.
API_TOKEN_FORM_NONCE_KEY = "hosting_api_token_form_nonce"


def render_account_page(account, settings, **extra):
    """Render the Account page for *account*.

    Shared with the API token form, which renders the page itself instead of
    redirecting so that a new token is shown once without being stored
    anywhere, not even in the session cookie.
    """
    current_session_id = get_hosting_session_id(
        session.get(_HOSTING_AUTH_SESSION_KEY)
    )
    active_sessions = []
    session_history = []
    if not session.get("hosting_impersonator_account_id"):
        for row in list_active_hosting_sessions(account["id"]):
            item = dict(row)
            item["is_current"] = item["id"] == current_session_id
            item["device_label"] = describe_session_user_agent(item["user_agent"])
            active_sessions.append(item)
        for row in list_hosting_session_history(account["id"]):
            item = dict(row)
            item["device_label"] = describe_session_user_agent(item["user_agent"])
            item["status"] = "ended" if item["revoked_at"] else "expired"
            item["ended_at"] = item["revoked_at"] or item["expires_at"]
            session_history.append(item)
    # Pending and denied accounts can open this page but cannot use the API,
    # so they are not offered tokens either. With the API switched off the
    # section stays as long as the account has tokens, so they can still be
    # revoked, but no new ones are offered.
    api_enabled = get_hosting_api_enabled()
    approved = account.get("approval_status", "approved") == "approved"
    api_tokens = list_api_tokens(account["id"]) if approved else []
    api_tokens_available = approved and (api_enabled or bool(api_tokens))
    # hosting_login_required sends an unverified account to the verification
    # page before it reaches the create form, so the form is not shown.
    api_token_email_unverified = bool(
        _email_policy_active() and _email_verification_required(settings)
        and account.get("email") and not account.get("email_verified_at")
    )
    api_token_form_nonce = None
    if (api_tokens_available and api_enabled and not api_token_email_unverified
            and not session.get("hosting_impersonator_account_id")):
        api_token_form_nonce = session.get(API_TOKEN_FORM_NONCE_KEY)
        if not api_token_form_nonce:
            api_token_form_nonce = secrets.token_urlsafe(24)
            session[API_TOKEN_FORM_NONCE_KEY] = api_token_form_nonce
    return render_template(
        "account.html", account=account, settings=settings,
        verification_cooldown=_verification_cooldown_remaining(account, settings),
        email_delivery_configured=email_is_configured(),
        active_sessions=active_sessions,
        session_history=session_history,
        api_tokens_available=api_tokens_available,
        api_enabled=api_enabled,
        api_token_email_unverified=api_token_email_unverified,
        api_token_form_nonce=api_token_form_nonce,
        api_tokens=api_tokens,
        api_token_scopes=api_scopes_for_account(account),
        api_token_limit=MAX_ACTIVE_API_TOKENS,
        api_token_name_max_length=API_TOKEN_NAME_MAX_LENGTH,
        **extra,
    )


def register_hosting_auth_routes(app):
    """Register authentication routes on *app*."""

    @app.context_processor
    def inject_account():
        """Inject the current account and theme variables into all templates."""
        account = get_current_account()
        settings = get_hosting_settings()
        from ..db import get_active_hosting_banners
        try:
            active_hosting_banners = get_active_hosting_banners(
                account["id"] if account else None
            )
        except Exception:
            # A platform restore can briefly replace the database with an
            # older schema before its migrations complete.
            active_hosting_banners = []

        # Resolve theme mode
        acct_theme = "default"
        if account:
            from ..db import get_account_theme_mode
            acct_theme = get_account_theme_mode(account["id"])
        effective_theme = "dark"
        if acct_theme in ("dark", "light"):
            effective_theme = acct_theme

        if effective_theme == "light":
            theme_primary = "#4b63b6"
            theme_secondary = "#ffffff"
            theme_accent = "#3553c7"
            theme_text = "#202534"
            theme_sidebar = "#e9edf5"
            theme_bg = "#f6f7fb"
        else:
            theme_primary = "#8fa0d4"
            theme_secondary = "#1e1e2c"
            theme_accent = "#7e9ada"
            theme_text = "#c8ccd8"
            theme_sidebar = "#1a1a24"
            theme_bg = "#16161f"

        # Global Arabic mirror (RTL) mode
        arabic_mirror = bool((settings or {}).get("arabic_mirror_enabled"))

        return {
            "current_account": account,
            "active_hosting_banners": active_hosting_banners,
            "hosting_impersonator": get_hosting_impersonator_account(),
            "hosting_effective_theme": effective_theme,
            "hosting_theme_primary": theme_primary,
            "hosting_theme_secondary": theme_secondary,
            "hosting_theme_accent": theme_accent,
            "hosting_theme_text": theme_text,
            "hosting_theme_sidebar": theme_sidebar,
            "hosting_theme_bg": theme_bg,
            "hosting_acct_theme": acct_theme,
            "hosting_arabic_mirror": arabic_mirror,
            "hosting_site_url": "https://{}".format(hosting_config.BASE_DOMAIN) if hosting_config.BASE_DOMAIN else "",
            "hosting_contact_email_required": bool(
                account
                and not account.get("email")
                and not account.get("email_flagged_invalid")
                and (settings or {}).get("email_required", 1)
                and (settings or {}).get("ask_email_existing_users", 1)
            ),
        }

    @app.route("/")
    def hosting_index():
        """Redirect to dashboard or login."""
        if "hosting_account_id" in session:
            return redirect(url_for("hosting_dashboard"))
        return redirect(url_for("hosting_login"))

    @app.route("/compliance")
    def hosting_compliance():
        """Redirect to the static-site Terms page (compliance hub)."""
        return _redirect_to_static_site("/terms/")

    @app.route("/terms")
    def hosting_terms():
        """Redirect to the static-site Terms of Service page."""
        return _redirect_to_static_site("/terms/")

    @app.route("/privacy")
    def hosting_privacy():
        """Redirect to the static-site Privacy Policy page."""
        return _redirect_to_static_site("/privacy/")

    @app.route("/forgot-username", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_forgot_username():
        """Send the username associated with an email address."""
        logged_in_redirect = _redirect_if_logged_in()
        if logged_in_redirect:
            return logged_in_redirect
        if request.method == "GET":
            return render_template("forgot_username.html")
        email_input = (request.form.get("email") or "").strip().lower()
        if not email_input:
            flash("Please enter your email address.", "error")
            return render_template("forgot_username.html")
        if not _EMAIL_RE.match(email_input):
            flash("Please enter a valid email address.", "error")
            return render_template("forgot_username.html")
        # Always show the same message regardless of whether the account
        # exists, to prevent email enumeration.
        generic_msg = (
            "If an account with that email exists, "
            "a message with your username has been sent."
        )
        account = get_account_by_email(email_input)
        if not account or (account.get("email") or "").lower() != email_input:
            flash(generic_msg, "info")
            return render_template("forgot_username.html")
        if not account.get("email_verified_at"):
            # Don't reveal that the account exists but email is unverified.
            flash(generic_msg, "info")
            return render_template("forgot_username.html")
        if not email_is_configured():
            flash(
                "Email delivery is not configured. "
                "Contact the platform administrator.",
                "error",
            )
            return render_template("forgot_username.html")
        ok, error = send_email(
            to=account["email"],
            subject="Your Username: BananaWiki",
            text=(
                "Hello,\n\n"
                "You requested a reminder of your BananaWiki username.\n\n"
                "Your username is: {}\n\n"
                "If you did not request this, you can safely ignore this email."
            ).format(account["username"]),
            html=render_email_html(
                title="Here is your username",
                eyebrow="ACCOUNT RECOVERY",
                text=(
                    "You requested a reminder of the username connected to this email address."
                    " If you did not make this request, no action is needed."
                ),
                detail_label="YOUR USERNAME",
                detail_value=account["username"],
            ),
        )
        if ok:
            flash(generic_msg, "info")
        else:
            flash(
                "The email could not be sent: {}. Please try again later.".format(error),
                "error",
            )
        return render_template("forgot_username.html")

    @app.route("/forgot-password", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_forgot_password():
        """Request a password reset link via email."""
        logged_in_redirect = _redirect_if_logged_in()
        if logged_in_redirect:
            return logged_in_redirect
        if request.method == "GET":
            return render_template("forgot_password.html")
        username = (request.form.get("username") or "").strip()
        email_input = (request.form.get("email") or "").strip().lower()
        if not username or not email_input:
            flash("Both username and email are required.", "error")
            return render_template("forgot_password.html")
        account = get_account_by_username(username)
        if not account or (account.get("email") or "").lower() != email_input:
            # Don't reveal whether the account exists
            flash("If an account with those details exists, a reset link has been sent.", "info")
            return render_template("forgot_password.html")
        if not account.get("email_verified_at"):
            flash("Your email must be verified before you can reset your password. Contact support.", "error")
            return render_template("forgot_password.html")
        if not email_is_configured():
            flash("Email delivery is not configured. Contact the platform administrator.", "error")
            return render_template("forgot_password.html")
        raw_token = secrets.token_urlsafe(32)
        expires = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        from ..db import issue_password_reset
        issue_password_reset(account["id"], raw_token, expires)
        reset_url = _public_portal_url("hosting_reset_password", token=raw_token)
        ok, error = send_email(
            to=account["email"],
            subject="Password Reset: BananaWiki",
            text="Hello {},\n\nReset your password by visiting:\n{}\n\nThis link expires in 1 hour. If you did not request this, ignore it.".format(
                account["username"], reset_url
            ),
            html=render_email_html(
                title="Reset your password",
                eyebrow="SECURE ACCOUNT RECOVERY",
                text=(
                    "Hello {},\n\nWe received a request to reset your BananaWiki Hosting password."
                    " Use the secure link below to choose a new one.\n\n"
                    "This link expires in 1 hour and can only be used once. If you did not request this,"
                    " you can safely ignore this email."
                ).format(account["username"]),
                action_url=reset_url,
                action_label="Reset password",
            ),
        )
        if ok:
            flash("A password reset link has been sent to your email. Check your inbox and spam folder.", "success")
        else:
            flash("The reset email could not be sent: {}. Please try again later.".format(error), "error")
        return render_template("forgot_password.html")

    @app.route("/reset-password", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_reset_password():
        """Consume a password reset token and set a new password."""
        logged_in_redirect = _redirect_if_logged_in()
        if logged_in_redirect:
            return logged_in_redirect
        token = request.args.get("token") or request.form.get("token") or ""
        if request.method == "GET":
            if not token:
                flash("Invalid or missing reset token.", "error")
                return redirect(url_for("hosting_login"))
            return render_template("reset_password.html", token=token)
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_new_password", "")
        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)
        if new_password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token)
        from ..db import consume_password_reset
        new_hash = generate_password_hash(new_password)
        if consume_password_reset(token, new_hash):
            flash("Password reset successfully. You can now log in.", "success")
            return redirect(url_for("hosting_login"))
        flash("Invalid, expired, or already-used reset token.", "error")
        return redirect(url_for("hosting_forgot_password"))

    @app.route("/login", methods=["GET", "POST"])
    def hosting_login():
        """Log in to the hosting portal."""
        logged_in_redirect = _redirect_if_logged_in()
        if logged_in_redirect:
            return logged_in_redirect
        if request.method == "GET":
            return render_template("login.html", bot_form_token=hosting_generate_form_token())

        ip = request.remote_addr or "unknown"
        recent = count_recent_hosting_login_attempts(ip, _LOGIN_WINDOW)
        if recent >= _LOGIN_MAX_ATTEMPTS:
            flash(
                "Too many login attempts. Please wait one minute and try again.",
                "error",
            )
            return render_template("login.html",
                                   bot_form_token=hosting_generate_form_token()), 429

        # Bot protection check
        if _hosting_bot_protection_enabled():
            blocked, _reason = hosting_check_bot_protection(request)
            if blocked:
                flash("Submission rejected. Please reload the page and try again.", "error")
                return render_template("login.html",
                                       bot_form_token=hosting_generate_form_token()), 400

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if len(password) > _PASSWORD_MAX_LENGTH:
            flash("Invalid username or password.", "error")
            return render_template("login.html",
                                   bot_form_token=hosting_generate_form_token()), 401

        account = get_account_by_username(username)
        if account is None:
            check_password_hash(_DUMMY_HASH, password)
            record_hosting_login_attempt(ip)
            flash("Invalid username or password.", "error")
            return render_template("login.html",
                                   bot_form_token=hosting_generate_form_token()), 401

        if not check_password_hash(account["password"], password):
            record_hosting_login_attempt(ip)
            flash("Invalid username or password.", "error")
            return render_template("login.html",
                                   bot_form_token=hosting_generate_form_token()), 401

        next_url = get_safe_next_url(request.args.get("next") or request.form.get("next"))
        if account.get("totp_enabled"):
            session.clear()
            session["hosting_mfa_pending_id"] = account["id"]
            session["hosting_mfa_started_at"] = time.time()
            session["hosting_mfa_session_version"] = int(account.get("session_version") or 0)
            session["hosting_mfa_next"] = next_url or ""
            return redirect(url_for("hosting_login_mfa"))
        return _complete_hosting_login(account, next_url)

    @app.route("/activation-pending")
    def hosting_activation_pending():
        """Show the activation-pending page."""
        account_id = session.get("hosting_account_id")
        account = get_account_by_id(account_id) if account_id else None
        if account and account.get("approval_status") == "approved":
            return redirect(url_for("hosting_dashboard"))
        if account and account.get("approval_status") == "denied":
            return redirect(url_for("hosting_activation_denied"))
        activation_required = get_hosting_activation_required()
        return render_template(
            "activation_pending.html",
            activation_required=activation_required,
            account=account,
        )

    @app.route("/activation-denied")
    def hosting_activation_denied():
        """Show the activation-denied page with deletion countdown."""
        account_id = session.get("hosting_account_id")
        remaining_seconds = None
        deletion_enabled = True
        account = None
        if account_id:
            account = get_account_by_id(account_id)
            if account and account.get("approval_status") == "approved":
                return redirect(url_for("hosting_dashboard"))
            if account and account.get("approval_status") == "denied":
                from datetime import datetime, timezone
                denied_at = account.get("denied_at")
                if denied_at:
                    try:
                        denied_dt = datetime.fromisoformat(denied_at)
                        if denied_dt.tzinfo is None:
                            denied_dt = denied_dt.replace(tzinfo=timezone.utc)
                        timeout = get_hosting_activation_denied_timeout_seconds()
                        deletion_enabled = get_hosting_activation_denied_deletion_enabled()
                        if deletion_enabled and timeout > 0:
                            expiry = denied_dt.timestamp() + timeout
                            remaining = expiry - datetime.now(timezone.utc).timestamp()
                            remaining_seconds = max(0, int(remaining))
                    except (ValueError, TypeError):
                        pass
        return render_template(
            "activation_denied.html",
            remaining_seconds=remaining_seconds,
            deletion_enabled=deletion_enabled,
            account=account,
        )

    @app.route("/pending-deletion")
    def hosting_pending_deletion():
        """Show a countdown page for accounts scheduled for deletion."""
        account_id = session.get("hosting_account_id")
        if not account_id:
            return redirect(url_for("hosting_login"))
        account = get_account_by_id(account_id)
        if not account:
            session.pop("hosting_account_id", None)
            return redirect(url_for("hosting_login"))
        if not account.get("pending_deletion"):
            return redirect(url_for("hosting_dashboard"))

        reason = account.get("pending_deletion_reason") or ""
        remaining_seconds = None
        pending_at = account.get("pending_deletion_at")
        timeout = int(account.get("pending_deletion_seconds") or 86400)
        if pending_at:
            try:
                from datetime import datetime, timezone
                pd_dt = datetime.fromisoformat(pending_at)
                if pd_dt.tzinfo is None:
                    pd_dt = pd_dt.replace(tzinfo=timezone.utc)
                expiry = pd_dt.timestamp() + timeout
                remaining = expiry - datetime.now(timezone.utc).timestamp()
                remaining_seconds = max(0, int(remaining))
            except (ValueError, TypeError):
                pass

        return render_template(
            "pending_deletion.html",
            reason=reason if reason else None,
            remaining_seconds=remaining_seconds,
        )

    @app.route("/account-suspended")
    def hosting_account_suspended():
        """Show a dedicated page for suspended hosting accounts."""
        account_id = session.get("hosting_account_id")
        if not account_id:
            return redirect(url_for("hosting_login"))
        account = get_account_by_id(account_id)
        if not account:
            session.pop("hosting_account_id", None)
            return redirect(url_for("hosting_login"))
        # Auto-unsuspend if timed suspension has expired
        if account["suspended"] and check_account_suspension_expired(account["id"]):
            session.clear()
            return redirect(url_for("hosting_login"))
        if not is_account_suspended(account["id"]):
            session.clear()
            return redirect(url_for("hosting_login"))

        # Build context for the template
        reason = account["suspend_reason"] if account["suspend_reason_visible"] else None
        suspended_until_str = None
        is_permanent = not account["suspended_until"]
        if account["suspended_until"] and account["suspend_time_visible"]:
            try:
                exp = datetime.fromisoformat(account["suspended_until"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                suspended_until_str = exp.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                pass
        elif account["suspended_until"] and not account["suspend_time_visible"]:
            # There is an expiry but the admin chose not to show it
            is_permanent = False

        return render_template(
            "account_suspended.html",
            reason=reason,
            suspended_until=suspended_until_str,
            is_permanent=is_permanent,
        ), 403

    @app.route("/signup", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_signup():
        """Create a new hosting portal account.

        Honours :func:`get_signup_mode`:

        * ``'open'``: anyone may create an account (default).
        * ``'invite'``: account creation and invite redemption commit
          together. The first account requires the installation token
          and becomes the administrator without an invite.
        * ``'closed'``: public signups are disabled and the form
          returns HTTP 403.  The very-first-account exception still
          applies so the platform can be bootstrapped.
        """
        # Logged-in users have no business on the signup form.
        logged_in_redirect = _redirect_if_logged_in()
        if logged_in_redirect:
            return logged_in_redirect
        # The first signup always becomes the admin.  Once at least one
        # admin exists, the signup_mode gate kicks in.
        is_first_signup = count_admin_accounts() == 0
        signup_mode = get_signup_mode() if not is_first_signup else "open"
        activation_required = (get_signup_mode() == "approval" or get_hosting_activation_required()) if not is_first_signup else False
        bootstrap_token = (request.values.get("bootstrap_token") or "").strip()
        if is_first_signup and not current_app.config.get("TESTING", False):
            expected = hosting_config.HOSTING_BOOTSTRAP_TOKEN
            if not expected:
                return (
                    "Initial admin signup is locked. Set HOSTING_BOOTSTRAP_TOKEN "
                    "and restart BananaWiki Hosting.",
                    503,
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
            if not hmac.compare_digest(expected.encode("utf-8"), bootstrap_token.encode("utf-8")):
                abort(404)

        if request.method == "GET":
            settings = get_hosting_settings()
            ask_email = bool(settings and settings.get("ask_email_new_signup", 0))
            use_case_required = bool(activation_required and settings and settings.get("signup_use_case_required"))
            return render_template(
                "signup.html",
                bot_form_token=hosting_generate_form_token(),
                signup_mode=signup_mode,
                is_first_signup=is_first_signup,
                activation_required=activation_required,
                signup_use_case_required=use_case_required,
                ask_email=ask_email,
                email_required=bool(ask_email and settings.get("email_required", 1)),
                bootstrap_token=bootstrap_token,
            )

        if signup_mode == "closed":
            flash(
                "Public signups are disabled on this platform. "
                "Please contact an administrator.",
                "error",
            )
            return render_template(
                "signup.html",
                bot_form_token=hosting_generate_form_token(),
                signup_mode=signup_mode,
                is_first_signup=is_first_signup,
                activation_required=activation_required,
                bootstrap_token=bootstrap_token,
            ), 403

        settings = get_hosting_settings()

        def _render(status):
            ask_email = bool(settings and settings.get("ask_email_new_signup", 0))
            use_case_required = bool(activation_required and settings and settings.get("signup_use_case_required"))
            return render_template(
                "signup.html",
                bot_form_token=hosting_generate_form_token(),
                signup_mode=signup_mode,
                is_first_signup=is_first_signup,
                activation_required=activation_required,
                signup_use_case_required=use_case_required,
                ask_email=ask_email,
                email_required=bool(ask_email and settings.get("email_required", 1)),
                bootstrap_token=bootstrap_token,
            ), status

        # Bot protection check
        if _hosting_bot_protection_enabled():
            blocked, _reason = hosting_check_bot_protection(request)
            if blocked:
                flash("Submission rejected. Please reload the page and try again.", "error")
                return _render(400)

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        invite_code = (request.form.get("invite_code") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        accepted_terms = request.form.get("accept_terms") == "1"

        if not accepted_terms and not current_app.config.get("TESTING"):
            flash("You must accept the Terms of Service and Privacy Policy.", "error")
            return _render(400)
        if email and not _EMAIL_RE.match(email):
            flash("Enter a valid email address.", "error")
            return _render(400)
        if (settings and settings.get("ask_email_new_signup", 0)
                and settings.get("email_required") and not email
                and not current_app.config.get("TESTING")):
            flash("An email address is required by this hosting platform.", "error")
            return _render(400)
        if email:
            existing_email = get_account_by_email(email)
            if existing_email:
                flash("That email address is already associated with an account.", "error")
                return _render(409)

        if not _USERNAME_RE.match(username):
            flash("Username must be 3-30 characters (letters, digits, hyphens, underscores).", "error")
            return _render(400)

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return _render(400)

        if len(password) > _PASSWORD_MAX_LENGTH:
            flash(f"Password cannot exceed {_PASSWORD_MAX_LENGTH} characters.", "error")
            return _render(400)

        if not _has_letter_and_number(password):
            flash("Password must include at least one letter and one number.", "error")
            return _render(400)

        if password != confirm:
            flash("Passwords do not match.", "error")
            return _render(400)

        if get_account_by_username(username):
            flash("That username is already taken.", "error")
            return _render(409)

        # Use-case validation (approval mode with signup_use_case_required)
        signup_use_case = (request.form.get("signup_use_case") or "").strip()
        use_case_required = bool(activation_required and settings and settings.get("signup_use_case_required"))
        if use_case_required and not is_first_signup:
            if len(signup_use_case) < 20 or len(signup_use_case) > 2000:
                flash("Describe your intended use in 20-2000 characters.", "error")
                return _render(400)

        hashed = generate_password_hash(password)
        try:
            aid, is_first = create_signup_account(
                username, hashed, invite_code=invite_code, email=email,
                terms_version=_TERMS_VERSION, signup_use_case=signup_use_case,
                bootstrap_allowed=is_first_signup,
            )
        except sqlite3.IntegrityError:
            flash("That username or email address is already in use.", "error")
            return _render(409)
        except ValueError as exc:
            flash(str(exc), "error")
            return _render(400)

        # Re-fetch account to check final approval status
        from ..db import get_account_by_id  # always available after insert
        final_account = get_account_by_id(aid)
        is_pending = bool(final_account and final_account.get("approval_status") == "pending")

        session.clear()
        session.permanent = True
        session["hosting_account_id"] = aid
        session["hosting_session_version"] = int((final_account or {}).get("session_version") or 0)
        if final_account:
            _establish_hosting_session(final_account, auth_method="signup")

        verification_required = _email_verification_required(settings)
        if current_app.config.get("TESTING"):
            update_hosting_account(
                aid,
                email_verified_at=datetime.now(timezone.utc).isoformat() if email else None,
            )
        elif verification_required:
            ok, message = _send_verification_email(get_account_by_id(aid), ignore_cooldown=True)
            flash(message, "success" if ok else "error")

        if is_first:
            flash("Account created. You are the platform admin.", "success")
        elif is_pending:
            flash("Account created. An administrator must approve your account before you can log in.", "success")
            # Immediate admin notice (best-effort; digest mode is handled
            # by the maintenance sweep instead).
            if get_approval_notify_mode() == "immediate":
                admin_email = get_approval_notify_email()
                if admin_email:
                    notify_admin_signup_pending(admin_email, final_account)
            if verification_required and not current_app.config.get("TESTING"):
                return redirect(url_for("hosting_email_verification_pending"))
            return redirect(url_for("hosting_activation_pending"))
        else:
            flash("Account created successfully.", "success")

        if verification_required and not current_app.config.get("TESTING"):
            return redirect(url_for("hosting_email_verification_pending"))
        return redirect(url_for("hosting_dashboard") if not is_pending else url_for("hosting_activation_pending"))

    @app.route("/account/theme", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=30, window=60)
    def hosting_set_theme():
        """Set the theme mode preference for the current account.

        Accepts ``theme_mode`` = ``'dark'`` | ``'light'`` | ``'default'``.
        """
        from ..db import set_account_theme_mode
        theme_mode = (request.form.get("theme_mode") or "").strip().lower()
        if theme_mode in ("dark", "light", "default"):
            set_account_theme_mode(session["hosting_account_id"], theme_mode)
        return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))

    @app.route("/account", methods=["GET", "POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_account():
        account = get_account_by_id(session["hosting_account_id"])
        settings = get_hosting_settings()
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            current_password = request.form.get("current_password") or ""
            if not _USERNAME_RE.match(username):
                flash("Username must be 3-30 characters (letters, digits, hyphens, underscores).", "error")
                return redirect(url_for("hosting_account"))
            existing = get_account_by_username(username)
            if existing and existing["id"] != account["id"]:
                flash("That username is already taken.", "error")
                return redirect(url_for("hosting_account"))
            if email and not _EMAIL_RE.match(email):
                flash("Enter a valid email address.", "error")
                return redirect(url_for("hosting_account"))
            if settings and settings.get("email_required") and not email:
                flash("An email address is required by this platform.", "error")
                return redirect(url_for("hosting_account"))
            identity_changed = username != account["username"] or email != (account.get("email") or "")
            if identity_changed and not check_password_hash(account["password"], current_password):
                flash("Enter your current password to change username or email.", "error")
                return redirect(url_for("hosting_account"))
            if email != (account.get("email") or ""):
                existing_email = get_account_by_email(email)
                if existing_email and existing_email["id"] != account["id"]:
                    flash("That email address is already associated with an account.", "error")
                    return redirect(url_for("hosting_account"))
                try:
                    update_hosting_account(
                        account["id"], username=username, email=email,
                        email_prompt_dismissed=1, email_verified_at=None,
                    )
                except sqlite3.IntegrityError:
                    flash("That username or email address is already in use.", "error")
                    return redirect(url_for("hosting_account"))
                ok, message = _send_verification_email(
                    get_account_by_id(account["id"]), ignore_cooldown=True,
                ) if _email_verification_required(settings) else (True, "Account details updated.")
                flash(message, "success" if ok else "error")
                if _email_verification_required(settings):
                    return redirect(url_for("hosting_email_verification_pending"))
            else:
                try:
                    update_hosting_account(
                        account["id"], username=username, email=email,
                        email_prompt_dismissed=1,
                    )
                except sqlite3.IntegrityError:
                    flash("That username or email address is already in use.", "error")
                    return redirect(url_for("hosting_account"))
                flash("Account details updated.", "success")
            return redirect(url_for("hosting_account"))
        return render_account_page(account, settings)

    @app.route("/account/sessions/<session_id>/revoke", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_revoke_session(session_id):
        """Terminate one hosting login session owned by the account."""
        if session.get("hosting_impersonator_account_id"):
            abort(403)
        account_id = session["hosting_account_id"]
        current_id = get_hosting_session_id(session.get(_HOSTING_AUTH_SESSION_KEY))
        if not revoke_hosting_session(account_id, session_id):
            abort(404)
        if session_id == current_id:
            session.clear()
            flash(translate("hosting.account.sessions.revoked"), "info")
            return redirect(url_for("hosting_login"))
        flash(translate("hosting.account.sessions.revoked"), "success")
        return redirect(url_for("hosting_account") + "#active-sessions")

    @app.route("/account/sessions/logout-all", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_logout_all_sessions():
        """Terminate every hosting login session, including legacy cookies."""
        if session.get("hosting_impersonator_account_id"):
            abort(403)
        account_id = session["hosting_account_id"]
        revoke_all_hosting_sessions(account_id)
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE accounts SET session_version=session_version+1 WHERE id=?",
                (account_id,),
            )
            conn.commit()
        session.clear()
        flash(translate("hosting.account.sessions.logged_out_everywhere"), "info")
        return redirect(url_for("hosting_login"))

    @app.route("/account/sessions/history/clear", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_clear_session_history():
        """Clear revoked and expired session records for the current account."""
        if session.get("hosting_impersonator_account_id"):
            abort(403)
        clear_hosting_session_history(session["hosting_account_id"])
        flash(translate("hosting.account.sessions.history_cleared"), "success")
        return redirect(url_for("hosting_account") + "#session-history")

    @app.route("/account/contact-email", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=8, window=300)
    def hosting_set_contact_email():
        """Collect the mandatory operator-contact address after login."""
        account = get_account_by_id(session["hosting_account_id"])
        settings = get_hosting_settings()
        email = (request.form.get("email") or "").strip().lower()
        current_password = request.form.get("current_password") or ""
        if not email or not _EMAIL_RE.match(email):
            flash("Enter a valid contact email address.", "error")
            return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))
        if not check_password_hash(account["password"], current_password):
            flash("Your current password is incorrect.", "error")
            return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))
        existing = get_account_by_email(email)
        if existing and existing["id"] != account["id"]:
            flash("That email address is already associated with an account.", "error")
            return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))
        try:
            update_hosting_account(
                account["id"], email=email, email_prompt_dismissed=1,
                email_verified_at=None,
            )
        except sqlite3.IntegrityError:
            flash("That email address is already associated with an account.", "error")
            return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))

        if _email_verification_required(settings):
            ok, message = _send_verification_email(
                get_account_by_id(account["id"]), ignore_cooldown=True,
            )
            flash(message, "success" if ok else "error")
            if ok:
                return redirect(url_for("hosting_email_verification_pending"))
        else:
            flash("Contact email saved. Verification is not required.", "success")
        return redirect(_safe_local_referrer() or url_for("hosting_dashboard"))

    @app.route("/verify-email")
    def hosting_verify_email():
        token = (request.args.get("token") or "").strip()
        account = verify_email_token(token) if token else None
        if account is None:
            flash("That verification link is invalid or has expired.", "error")
            return redirect(url_for("hosting_email_verification_pending") if session.get("hosting_account_id") else url_for("hosting_login"))
        flash("Email address verified successfully.", "success")
        if session.get("hosting_account_id") == account["id"]:
            if account.get("approval_status") == "pending":
                return redirect(url_for("hosting_activation_pending"))
            return redirect(url_for("hosting_dashboard"))
        return redirect(url_for("hosting_login"))

    @app.route("/account/verify-email")
    @hosting_login_required
    def hosting_email_verification_pending():
        account = get_account_by_id(session["hosting_account_id"])
        if account.get("email_verified_at"):
            return redirect(url_for("hosting_dashboard"))
        settings = get_hosting_settings()
        return render_template(
            "verify_email.html",
            account=account,
            cooldown=_verification_cooldown_remaining(account, settings),
            email_delivery_configured=email_is_configured(),
        )

    @app.route("/account/resend-verification", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_resend_verification():
        account = get_account_by_id(session["hosting_account_id"])
        if account.get("email_verified_at"):
            flash("Your email is already verified.", "info")
            return redirect(url_for("hosting_account"))
        ok, message = _send_verification_email(account)
        flash(message, "success" if ok else "error")
        return redirect(url_for("hosting_email_verification_pending"))

    @app.route("/account/email-flagged")
    @hosting_login_required
    def hosting_email_flagged():
        """Show a page explaining that the user's email has been flagged
        as invalid by an administrator, and collect a new address."""
        account = get_account_by_id(session["hosting_account_id"])
        if not account.get("email_flagged_invalid"):
            return redirect(url_for("hosting_dashboard"))
        return render_template(
            "email_flagged.html",
            account=account,
            flag_reason=account.get("email_flag_reason") or "",
            reason_visible=bool(account.get("email_flag_reason_visible")),
        )

    @app.route("/account/email-flagged", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=8, window=300)
    def hosting_submit_flagged_email():
        """Accept a new email from a user whose previous address was
        flagged as invalid.  Clears the flag and saves the new email."""
        account = get_account_by_id(session["hosting_account_id"])
        if not account.get("email_flagged_invalid"):
            return redirect(url_for("hosting_dashboard"))
        email = (request.form.get("email") or "").strip().lower()
        if not email or not _EMAIL_RE.match(email):
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("hosting_email_flagged"))
        # Optionally block re-entry of the same address that was flagged
        # (controlled by the email_flag_block_reentry hosting setting)
        settings = get_hosting_settings()
        block_reentry = bool(settings and settings.get("email_flag_block_reentry"))
        if block_reentry:
            previous = (account.get("email_flagged_previous") or "").lower()
            if previous and email == previous:
                flash(
                    "This is the address that was flagged as invalid. "
                    "Please use a different email.",
                    "error",
                )
                return redirect(url_for("hosting_email_flagged"))
        # Check for duplicate verified email
        existing = get_account_by_email(email)
        if existing and existing["id"] != account["id"]:
            flash("This email is already registered to another account.", "error")
            return redirect(url_for("hosting_email_flagged"))
        # Save new email and clear flag
        update_hosting_account(
            account["id"],
            email=email,
            email_verified_at=None,
        )
        clear_email_flag(account["id"])
        # Send verification if required
        settings = get_hosting_settings()
        if _email_verification_required(settings):
            refreshed = get_account_by_id(account["id"])
            ok, message = _send_verification_email(refreshed, ignore_cooldown=True)
            flash(message, "success" if ok else "error")
            if ok:
                return redirect(url_for("hosting_email_verification_pending"))
        flash("Your new email address has been saved. Thank you.", "success")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/account/export", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=3, window=300)
    def hosting_export_account():
        account = dict(get_account_by_id(session["hosting_account_id"]))
        for secret_field in (
            "password", "email_verification_token_hash",
            "password_reset_token_hash",
            # Authenticator secrets and recovery material stay private.
            "totp_secret_encrypted", "totp_recovery_hashes",
            "totp_enabled", "totp_enabled_at", "totp_last_counter",
        ):
            account.pop(secret_field, None)
        instances = [dict(row) for row in get_instances_for_account(account["id"])]
        for instance in instances:
            instance.pop("admin_password_plain", None)
            instance.pop("oauth_client_secret_hash", None)
        payload = json.dumps({"exported_at": datetime.now(timezone.utc).isoformat(),
                              "account": account, "instances": instances},
                             indent=2, ensure_ascii=False)
        response = Response(payload, content_type="application/json; charset=utf-8")
        response.headers["Content-Disposition"] = "attachment; filename=bananawiki-account-data.json"
        return response

    @app.route("/logout", methods=["POST"])
    def hosting_logout():
        """Log out of the hosting portal."""
        revoke_hosting_session_token(session.get(_HOSTING_AUTH_SESSION_KEY))
        session.clear()
        return redirect(url_for("hosting_login"))

    @app.route("/language", methods=["POST"])
    @hosting_rate_limit(max_requests=30, window=60)
    def hosting_change_language():
        """Update the per-session interface language preference.

        Hosting users can switch between English (``en``) and Italian
        (``it``) only: the two built-in interface languages.  Any other
        value is rejected and the session preference is cleared so the
        portal falls back to ``Accept-Language`` / English defaults.
        The choice is stored in ``session["interface_language"]`` so it
        is scoped to the current visitor only and does not affect other
        accounts.
        """
        language = (request.form.get("language") or "").strip().lower()
        if language in BUILTIN_INTERFACE_LANGUAGES:
            session["interface_language"] = language
        else:
            session.pop("interface_language", None)
        return redirect(_safe_local_referrer() or url_for("hosting_index"))

    @app.route("/account/delete", methods=["POST"])
    @app.route("/settings/delete", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_delete_account():
        """Delete the current account and terminate all instances."""
        account_id = session["hosting_account_id"]

        account = get_account_by_id(account_id)
        current_password = request.form.get("current_password") or ""
        if not account or not check_password_hash(account["password"], current_password):
            flash("Enter your current password to delete the account.", "error")
            return redirect(url_for("hosting_account"))
        if account and account["is_admin"] and count_admin_accounts() <= 1:
            flash("You are the last admin. Promote another account before deleting yours.", "error")
            return redirect(url_for("hosting_dashboard"))

        instances = get_instances_for_account(account_id)
        for inst in instances:
            terminate_instance(inst["id"])

        delete_account(account_id)
        session.clear()
        flash("Your account and all instances have been deleted.", "success")
        return redirect(url_for("hosting_login"))

    @app.route("/account/change-password", methods=["POST"])
    @app.route("/settings/change-password", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_change_password():
        """Change the current account's password."""
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        if account is None:
            session.pop("hosting_account_id", None)
            return redirect(url_for("hosting_login"))

        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm_pw = request.form.get("confirm_new_password") or ""

        if len(current_pw) > _PASSWORD_MAX_LENGTH:
            check_password_hash(_DUMMY_HASH, "")  # constant-time guard
            flash("Current password is incorrect.", "error")
            return redirect(url_for("hosting_dashboard"))

        if not check_password_hash(account["password"], current_pw):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("hosting_dashboard"))

        if len(new_pw) < 8:
            flash("New password must be at least 8 characters.", "error")
            return redirect(url_for("hosting_dashboard"))

        if len(new_pw) > _PASSWORD_MAX_LENGTH:
            flash(f"New password cannot exceed {_PASSWORD_MAX_LENGTH} characters.", "error")
            return redirect(url_for("hosting_dashboard"))

        if not _has_letter_and_number(new_pw):
            flash("New password must include at least one letter and one number.", "error")
            return redirect(url_for("hosting_dashboard"))

        if new_pw != confirm_pw:
            flash("New passwords do not match.", "error")
            return redirect(url_for("hosting_dashboard"))

        new_version = change_hosting_password_preserving_session(
            account_id,
            generate_password_hash(new_pw),
            session.get(_HOSTING_AUTH_SESSION_KEY),
        )
        if new_version is None:
            session.clear()
            return redirect(url_for("hosting_login"))
        session["hosting_session_version"] = new_version
        flash("Password changed successfully.", "success")
        return redirect(url_for("hosting_dashboard"))
