"""Password sign-in shared by /login, /admin and /session-conflict/force.

Failed attempts are counted per client address and per account name in the
shared ``rate_limit_hits`` table. A successful sign-in only clears the
counter of the account that signed in, so a valid account cannot be used to
reset the budget for guessing other accounts' passwords (audit H3).
"""

from __future__ import annotations

from typing import Any

from flask import current_app, redirect, url_for
from werkzeug.wrappers import Response

from ....core import passwords
from ....core.ratelimit import SqlLimiter
from ....core.web import client_ip, safe_next
from ... import accounts, auth, settings
from ...db import db
from ...registry import emit

WINDOW_SECONDS = 15 * 60
MAX_PER_IP = 20
MAX_PER_ACCOUNT = 8
# Failures for one account from all addresses together. Much higher than the per-source limit:
# with a low shared limit, anyone could keep the owner out by failing 8 times every 15 minutes.
MAX_PER_ACCOUNT_TOTAL = 100


class Refused(Exception):
    """Sign-in refused; ``key`` is a translation key, ``status`` the HTTP status to answer with."""

    def __init__(self, key: str, status: int):
        super().__init__(key)
        self.key = key
        self.status = status


def _limiter() -> SqlLimiter:
    return SqlLimiter(db.session)


def verify(username: str, password: str, *, admin_only: bool = False) -> dict[str, Any]:
    """Return the account for these credentials or raise :class:`Refused`."""
    ip = client_ip()
    account_key = username.lower()
    source_key = f"{account_key}\n{ip}"
    limiter = _limiter()
    reservations = []
    with db.transaction():
        for key, bucket, limit in ((ip, "login:ip", MAX_PER_IP),
                                   (source_key, "login:account-ip", MAX_PER_ACCOUNT),
                                   (account_key, "login:account", MAX_PER_ACCOUNT_TOTAL)):
            reservation = limiter.reserve(key, bucket, limit, WINDOW_SECONDS)
            if reservation is None:
                # Roll back the earlier reservations when any bucket is full.
                raise Refused("auth.error.too_many_attempts", 429)
            reservations.append(reservation)
    user = accounts.by_username(username) if username and len(password) <= passwords.MAX_LENGTH else None
    if user is None:
        passwords.burn_time(password)
    if user is None or not passwords.verify_password(user["password"], password):
        current_app.logger.info("Failed sign-in for %r from %s", username, ip)
        raise Refused("auth.error.invalid_credentials", 401)
    with db.transaction():
        for reservation in reservations:
            limiter.release(reservation)
        limiter.clear(source_key, "login:account-ip")
    if admin_only and not auth.is_admin(user):
        raise Refused("auth.admin_login.not_admin", 403)
    return user


def landing_url(user: dict[str, Any], next_url: str | None, *, default: str = "/") -> str:
    """Where a freshly signed-in account goes first."""
    if auth.account_block(user):
        return url_for("auth.account_status")
    if user.get("force_password_change"):
        return url_for("auth.force_password_change")
    if user.get("onboarding_required"):
        return url_for("auth.onboarding")
    target = safe_next("", next_url)
    if user.get("intro_required"):
        return url_for("auth.intro", next=target or None)
    if not target and settings.get("login_app_selector"):
        return url_for("auth.app_selector")
    return target or default


def finish(user: dict[str, Any], *, remember: bool, next_url: str | None, method: str = "password",
           default: str = "/") -> Response:
    """Start the session (unless maintenance keeps this account out) and redirect."""
    if settings.maintenance_active() and not auth.is_admin(user):
        return redirect(url_for("auth.maintenance"))
    auth.start_session(user, remember=remember, method=method)
    emit("user.login", user=user)
    return redirect(landing_url(auth.current_user() or user, next_url, default=default))
