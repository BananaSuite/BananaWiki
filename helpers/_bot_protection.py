"""
Bot protection helpers: honeypot field + form-timing validation.

No external services are used.  Two lightweight techniques are combined:

1. **Honeypot field**: a hidden text field (``name="website"``) that is
   invisible to human visitors via CSS but filled in by many automated
   form-submission bots.  A non-empty value is an immediate bot signal.

2. **Timing check**: each form render embeds a server-signed timestamp.
   If the form is submitted in under ``_MIN_FORM_SECONDS`` the request is
   treated as automated (real users always take longer than a fraction of
   a second to fill in a form).

Usage in route handlers
-----------------------
On GET render a fresh form token:

    token = generate_form_token()

Pass it to the template as ``bot_form_token``.  The template includes:

    <input type="hidden" name="_form_time" value="{{ bot_form_token }}">
    <!-- honeypot -->
    <div class="hp-field" style="display:none!important" aria-hidden="true">
        <input type="text" name="website" value="" autocomplete="off" tabindex="-1">
    </div>

On POST validate before any other processing:

    blocked, reason = check_bot_protection(request)
    if blocked:
        flash("Submission rejected. Please try again.", "error")
        return render_template(..., bot_form_token=generate_form_token()), 400

Skipping in tests
-----------------
Bot protection is automatically skipped when
``current_app.config.get("TESTING")`` is truthy (the test suite sets
``app.config["TESTING"] = True``) or when ``WTF_CSRF_ENABLED`` is
``False`` (another test-mode flag).
"""

import hashlib
import hmac
import os
import time

from flask import current_app


# Minimum seconds that must elapse between the form being rendered (GET)
# and submitted (POST).  Keep this low so password managers and local
# first-run setup flows do not feel broken, while still catching instant bots.
try:
    _MIN_FORM_SECONDS = max(0.0, float(os.environ.get("BW_MIN_FORM_SECONDS", "0.4")))
except (TypeError, ValueError):
    _MIN_FORM_SECONDS = 0.4

# Maximum age of a valid form token (seconds).  Prevents replay of very
# old tokens; 4 hours gives plenty of time for genuine users.
_MAX_FORM_SECONDS = 4 * 3600

# Honeypot field name: invisible to humans, often filled by bots.
HONEYPOT_FIELD = "website"

# Hidden timestamp-token field name.
FORM_TIME_FIELD = "_form_time"


# Internal helpers

def _get_secret() -> bytes:
    """Return the Flask application secret key as *bytes*."""
    key = current_app.secret_key
    if isinstance(key, str):
        return key.encode("utf-8")
    return bytes(key)


def _sign(timestamp: str) -> str:
    """Return a short HMAC-SHA256 signature for *timestamp*."""
    return hmac.new(_get_secret(), timestamp.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _is_testing() -> bool:
    """Return ``True`` when running inside the test suite."""
    try:
        if current_app.config.get("TESTING"):
            return True
        if not current_app.config.get("WTF_CSRF_ENABLED", True):
            return True
    except RuntimeError:
        pass
    return False


# Public API

def generate_form_token() -> str:
    """Return a signed timestamp token to embed in a form.

    Format: ``<unix_timestamp>.<hmac_signature>``
    """
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def check_bot_protection(request) -> tuple[bool, str]:
    """Validate bot-protection fields from *request.form*.

    Returns ``(blocked, reason)`` where *blocked* is ``True`` when the
    request looks automated.  Always returns ``(False, "")`` in test mode.
    """
    if _is_testing():
        return False, ""

    # Honeypot: real users never fill this field.
    honeypot = request.form.get(HONEYPOT_FIELD, "")
    if honeypot:
        return True, "honeypot"

    # Timing: bots submit forms in milliseconds; real users take seconds.
    raw_token = request.form.get(FORM_TIME_FIELD, "")
    if not raw_token:
        return True, "missing_token"

    try:
        ts_str, sig = raw_token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return True, "invalid_token"

    # Verify HMAC signature to prevent token forgery.
    expected = _sign(ts_str)
    if not hmac.compare_digest(sig, expected):
        return True, "invalid_token"

    elapsed = time.time() - ts
    if elapsed < _MIN_FORM_SECONDS:
        return True, "too_fast"

    if elapsed > _MAX_FORM_SECONDS:
        return True, "expired_token"

    return False, ""


def is_bot_protection_enabled() -> bool:
    """Return ``True`` when bot protection is active for this wiki instance.

    Reads the ``bot_protection_enabled`` column from ``site_settings``.
    Falls back to ``True`` (enabled) if the setting cannot be read.
    """
    try:
        import db
        settings = db.get_site_settings()
        if settings is None:
            return True
        return bool(settings.get("bot_protection_enabled", 1))
    except Exception:
        return True
