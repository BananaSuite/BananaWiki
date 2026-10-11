"""Password sign-in shared by /login, /admin and /session-conflict/force.

Failed attempts are counted per client network (an IPv6 client's /64), per
account name and source, and per account in the shared ``rate_limit_hits``
table. A successful sign-in only clears the counter of the account that
signed in, so a valid account cannot be used to reset the budget for
guessing other accounts' passwords (audit H3).

A browser that has signed in to an account before carries a device cookie
for it (:mod:`.devices`: signed with the wiki's secret and the account's
device epoch, which a new password and every change to two-step verification
retire). Its failures are counted apart from everyone else's, so strangers
who use up the account's shared budget cannot lock the owner out (as in the
hosting portal, AUDIT #29).

Accounts with two-step verification (:mod:`.two_step`) answer a code after
the password, or after the hosting portal's sign-in: :func:`finish` keeps
the sign-in pending (in the session cookie, for five minutes, bound to the
password and the two-step set-up) and :func:`finish_second_step` completes
it. Wrong codes are limited (:func:`check_code`):

* per account and known device, or per account for every other browser: only
  someone holding the password reaches that step, and they cannot use up a
  known browser's attempts. While two-step verification is on, a device
  cookie is issued only once both steps passed, so nobody can collect cookies
  to multiply the limits;
* with a growing wait: after ``TWO_STEP_DELAY_AFTER`` wrong codes in a day
  from one browser (or from the browsers without a cookie together), each
  further code waits twice as long as the one before;
* per account from all browsers together (``TWO_STEP_MAX_PER_ACCOUNT_DAY``
  a day), a ceiling the browsers without a cookie cannot reach on their own.

After ``TWO_STEP_ALERT_AFTER`` wrong codes at sign-in in a day the owner gets
a notice (once a day), since someone else may know the password.
"""

from __future__ import annotations

import hmac
import math
import time
from typing import Any

from flask import current_app, redirect, session, url_for
from werkzeug.wrappers import Response

from ....core import passwords
from ....core.ratelimit import SqlLimiter
from ....core.timeutil import parse, utcnow
from ....core.web import client_ip, client_network, safe_next
from ... import accounts, attention, auth, settings
from ...db import db
from ...registry import emit
from ..audit.service import record
from . import devices, two_step

WINDOW_SECONDS = 15 * 60
MAX_PER_IP = 20
MAX_PER_ACCOUNT = 8
# Failures for one account from all addresses together. Much higher than the per-source limit:
# with a low shared limit, anyone could keep the owner out by failing 8 times every 15 minutes.
# Browsers that signed in to the account before have their own budget instead (MAX_PER_ACCOUNT).
MAX_PER_ACCOUNT_TOTAL = 100
# Wrong two-step codes per account and known device (or per account for other
# browsers): enough for typos, too few to guess one of a million codes.
TWO_STEP_WINDOW_SECONDS = 5 * 60
TWO_STEP_MAX = 10
TWO_STEP_MAX_PER_DAY = 50
# From the same browsers, once this many wrong codes came in a day, each
# further code waits TWO_STEP_DELAY_SECONDS, then twice as long each time, up
# to TWO_STEP_DELAY_MAX_SECONDS.
TWO_STEP_DELAY_AFTER = 10
TWO_STEP_DELAY_SECONDS = 30
TWO_STEP_DELAY_MAX_SECONDS = 60 * 60
# Wrong codes for one account from every browser together in a day. Above
# TWO_STEP_MAX_PER_DAY, so the browsers without a device cookie (everyone who
# merely knows the password) cannot reach it and lock out the owner's browsers.
TWO_STEP_MAX_PER_ACCOUNT_DAY = 100
# Wrong sign-in codes for one account, from every browser, before its owner is told (once a day).
TWO_STEP_ALERT_AFTER = 5
TWO_STEP_ALERT_WINDOW_SECONDS = 86400
TWO_STEP_PENDING_KEY = "_two_step_pending"
TWO_STEP_PENDING_SECONDS = 5 * 60


class Refused(Exception):
    """Sign-in refused; ``key`` is a translation key (with ``values``), ``status`` the HTTP status to answer with."""

    def __init__(self, key: str, status: int, **values: Any):
        super().__init__(key)
        self.key = key
        self.status = status
        self.values = values


def _limiter() -> SqlLimiter:
    return SqlLimiter(db.session)


# ── Password check ────────────────────────────────────────────────────────────


def _carry_over_api_tokens(user_id: str, old_hash: str, new_hash: str) -> None:
    """API tokens are stamped with the password they were issued under; a re-hash keeps the password."""
    from ..api_service.tokens import credential_stamp

    db.execute("UPDATE api_service__tokens SET credential_stamp = ? WHERE user_id = ? AND credential_stamp = ?",
               (credential_stamp(new_hash), user_id, credential_stamp(old_hash)))


def _upgrade_hash(user: dict[str, Any], password: str) -> dict[str, Any]:
    """Re-hash a password stored with an older method or cost, once it is known to be right.

    The password stays the same, so what was issued under it carries over:
    the account's API tokens and its device cookies keep working.
    """
    method = current_app.config["BW"].password_hash_method
    if not passwords.needs_rehash(user["password"], method):
        return user
    upgraded = passwords.hash_password(password, method)
    with db.transaction():
        # Only when nothing changed the password meanwhile.
        changed = db.execute("UPDATE users SET password = ? WHERE id = ? AND password = ?",
                             (upgraded, user["id"], user["password"])).rowcount
        if changed:
            devices.carry_over(user["id"], user["password"], upgraded)
            _carry_over_api_tokens(user["id"], user["password"], upgraded)
    current = accounts.by_id(user["id"])
    if current is None:
        raise Refused("auth.error.invalid_credentials", 401)
    if not changed and not passwords.verify_password(current["password"], password):
        # Another sign-in re-hashed the same password (fine), or it was changed meanwhile (not).
        raise Refused("auth.error.invalid_credentials", 401)
    return current


def verify(username: str, password: str, *, admin_only: bool = False) -> dict[str, Any]:
    """Return the account for these credentials or raise :class:`Refused`."""
    ip = client_ip()
    network = client_network()
    account_key = username.lower()
    source_key = f"{account_key}\n{network}"
    usable = bool(username) and len(password) <= passwords.MAX_LENGTH
    user = accounts.by_username(username) if usable else None
    device = devices.nonce(user) if user is not None else None
    buckets = [(network, "login:ip", MAX_PER_IP), (source_key, "login:account-ip", MAX_PER_ACCOUNT)]
    if device:
        # Failures from a browser that signed in to this account before are
        # counted apart from everyone else's, so strangers cannot lock it out.
        buckets.append((f"{user['id']}\n{device}", "login:device", MAX_PER_ACCOUNT))  # type: ignore[index]
    else:
        buckets.append((account_key, "login:account", MAX_PER_ACCOUNT_TOTAL))
    limiter = _limiter()
    reservations = []
    with db.transaction():
        for key, bucket, limit in buckets:
            reservation = limiter.reserve(key, bucket, limit, WINDOW_SECONDS)
            if reservation is None:
                # Roll back the earlier reservations when any bucket is full.
                raise Refused("auth.error.too_many_attempts", 429)
            reservations.append(reservation)
    method = current_app.config["BW"].password_hash_method
    if user is None:
        passwords.burn_time(password, method)
    if user is None or not passwords.verify_password(user["password"], password):
        # An unknown name is often a password typed into the wrong field, so it is not recorded.
        current_app.logger.info("Failed sign-in for %s from %s", f"{username!r}" if user else "an unknown account", ip)
        raise Refused("auth.error.invalid_credentials", 401)
    with db.transaction():
        for reservation in reservations:
            limiter.release(reservation)
        limiter.clear(source_key, "login:account-ip")
    if admin_only and not auth.is_admin(user):
        raise Refused("auth.admin_login.not_admin", 403)
    return _upgrade_hash(user, password)


def landing_url(user: dict[str, Any], next_url: str | None, *, default: str = "/") -> str:
    """Where a freshly signed-in account goes first."""
    if auth.account_block(user):
        return url_for("auth.account_status")
    if user.get("force_password_change"):
        return url_for("auth.force_password_change")
    if user.get("onboarding_required"):
        return url_for("auth.onboarding")
    if two_step.missing(user):
        return url_for("auth.two_step_settings")
    target = safe_next("", next_url)
    if user.get("intro_required"):
        return url_for("auth.intro", next=target or None)
    if not target and settings.get("login_app_selector"):
        return url_for("auth.app_selector")
    return target or default


def finish(user: dict[str, Any], *, remember: bool, next_url: str | None, method: str = "password",
           default: str = "/") -> Response:
    """Start the session (unless maintenance keeps this account out) and redirect.

    With two-step verification on, the sign-in waits for a code first
    (:func:`finish_second_step`).
    """
    if settings.maintenance_active() and not auth.is_admin(user):
        return redirect(url_for("auth.maintenance"))
    if two_step.enabled(user["id"]):
        language = session.get("interface_language")
        session.clear()  # whoever was signed in in this browser is no longer
        if language:
            session["interface_language"] = language
        session[TWO_STEP_PENDING_KEY] = {
            "user_id": user["id"], "at": time.time(), "fingerprint": two_step.fingerprint(user),
            "remember": bool(remember), "next": (next_url or "")[:2000], "method": method, "default": default,
        }
        return redirect(url_for("auth.two_step_login"))
    return _complete(user, remember=remember, next_url=next_url, method=method, default=default)


def _complete(user: dict[str, Any], *, remember: bool, next_url: str | None, method: str,
              default: str) -> Response:
    auth.start_session(user, remember=remember, method=method)
    emit("user.login", user=user)
    signed_in = auth.current_user() or user
    response = redirect(landing_url(signed_in, next_url, default=default))
    devices.set_cookie(response, signed_in)
    return response


# ── Second step ───────────────────────────────────────────────────────────────


def pending_second_step() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(account, pending sign-in)`` while this browser owes a code, else None.

    The sign-in lapses after a few minutes, and when the password or the
    two-step set-up changed since the password was checked.
    """
    pending = session.get(TWO_STEP_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    try:
        started = float(pending.get("at", 0))
    except (TypeError, ValueError):
        return None
    if not 0 <= time.time() - started <= TWO_STEP_PENDING_SECONDS:
        return None
    user = accounts.by_id(pending.get("user_id"))
    if user is None or not hmac.compare_digest(str(pending.get("fingerprint", "")), two_step.fingerprint(user)):
        return None
    return user, pending


def _note_wrong_code(user: dict[str, Any]) -> None:
    """Count a wrong sign-in code for the account; tell the owner once a day after several."""
    limiter, key = _limiter(), str(user["id"])
    with db.transaction():
        limiter.record(key, "login:2fa-wrong")
        due = (limiter.count(key, "login:2fa-wrong", TWO_STEP_ALERT_WINDOW_SECONDS) >= TWO_STEP_ALERT_AFTER
               and limiter.hit(key, "login:2fa-alerted", 1, TWO_STEP_ALERT_WINDOW_SECONDS))
    if due:
        attention.warned(user["id"], "auth.two_step", endpoint="auth.two_step_settings")


def _wait_seconds(limiter: SqlLimiter, key: str) -> int:
    """How long *key* must still wait before its next code (0: it may try now)."""
    wrong = limiter.count(key, "login:2fa-day", 86400)
    if wrong < TWO_STEP_DELAY_AFTER:
        return 0
    delay = min(TWO_STEP_DELAY_SECONDS * 2 ** min(wrong - TWO_STEP_DELAY_AFTER, 32), TWO_STEP_DELAY_MAX_SECONDS)
    last = parse(limiter.latest(key, "login:2fa-day"))
    elapsed = (utcnow() - last).total_seconds() if last is not None else delay
    return max(0, math.ceil(delay - elapsed))


def check_code(user: dict[str, Any], code: str | None, *, sign_in: bool = True) -> str:
    """Accept a two-step code of *user*: ``"totp"`` or ``"recovery_code"``, else :class:`Refused`.

    Used at sign-in and before changes in the settings (``sign_in=False``:
    the signed-in owner's own typos never alert them). Only wrong codes
    count (see the module docstring for the limits); a browser that signed
    in to the account with the current two-step set-up has its own budget.
    """
    device = devices.nonce(user)
    key = f"{user['id']}\n{device}" if device else str(user["id"])
    limiter = _limiter()
    reservations = []
    with db.transaction():
        wait = _wait_seconds(limiter, key)
        if wait:
            raise Refused("auth.two_step.wait", 429, minutes=max(1, math.ceil(wait / 60)))
        for bucket_key, bucket, limit, window, refusal in (
                (key, "login:2fa", TWO_STEP_MAX, TWO_STEP_WINDOW_SECONDS, "auth.two_step.too_many"),
                (key, "login:2fa-day", TWO_STEP_MAX_PER_DAY, 86400, "auth.two_step.too_many"),
                (str(user["id"]), "login:2fa-account", TWO_STEP_MAX_PER_ACCOUNT_DAY, 86400,
                 "auth.two_step.too_many_today")):
            reservation = limiter.reserve(bucket_key, bucket, limit, window)
            if reservation is None:
                raise Refused(refusal, 429)
            reservations.append(reservation)
    method = two_step.verify(user["id"], code)
    if method is None:
        current_app.logger.info("Wrong two-step code for %r from %s", user["username"], client_ip())
        if sign_in:
            _note_wrong_code(user)
        raise Refused("auth.two_step.invalid_code", 401)
    with db.transaction():
        for reservation in reservations:
            limiter.release(reservation)
    return method


def finish_second_step(user: dict[str, Any], pending: dict[str, Any], method: str) -> Response:
    """Complete a sign-in whose code *method* was accepted."""
    session.pop(TWO_STEP_PENDING_KEY, None)
    if settings.maintenance_active() and not auth.is_admin(user):
        return redirect(url_for("auth.maintenance"))
    response = _complete(user, remember=bool(pending.get("remember")), next_url=pending.get("next") or "",
                         method=f"{pending.get('method') or 'password'}+{method}"[:40],
                         default=str(pending.get("default") or "/"))
    if method == "recovery_code":
        record("user.two_step_recovery_code_used", actor_id=user["id"], target_type="user", target_id=user["id"],
               details={"username": user["username"]})
        # After the session started: starting it clears earlier messages.
        left = (two_step.state(user["id"]) or {}).get("codes_left", 0)
        auth.flash_t("auth.two_step.recovery_used", "warning", count=left)
    return response
