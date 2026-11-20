"""Route: /login/recovery, the administrators' own way in while only hosting accounts may sign in.

With the sign-in mode ``platform`` (:mod:`.signin_modes`) nobody signs in
with a wiki password, so an outage of the hosting portal, or a mistaken
switch, would keep everyone out. Administrators (``admin`` and ``owner``
roles) can still sign in here with their wiki password and, when they use
it, their two-step code (the ordinary password check and second step of
:mod:`.signin`). The page is not linked from the sign-in page; the site
settings mention it.

* Every other account gets the answer of a wrong password, from the same
  check (:func:`signin.verify` with ``eligible``), so the page tells nobody
  which accounts exist or which password is right.
* At most ``MAX_PER_IP`` attempts per client network in ``WINDOW_SECONDS``
  (successful or not, shared by every worker), on top of the sign-in limits.
* Every attempt, accepted or refused, is written to the audit log, even while
  the audit log is switched off.

In the other modes the page sends visitors to the ordinary sign-in.
"""

from __future__ import annotations

from typing import Any

from flask import current_app, redirect, render_template, request, url_for

from ....core.ratelimit import SqlLimiter
from ....core.web import client_ip, client_network, safe_next
from ... import accounts, auth
from ...db import db
from ..audit.service import record
from . import signin, signin_modes, two_step
from .blueprint import SIGN_IN_GATES, bp

WINDOW_SECONDS = 60 * 60
MAX_PER_IP = 10
TEMPLATE = "auth/login_recovery.html"


def _audit(action: str, user: dict[str, Any] | None, **details: Any) -> None:
    if user is not None:
        details["username"] = user["username"]
    record(action, actor_id=user["id"] if user is not None and action == "user.recovery_sign_in" else None,
           target_type="user" if user is not None else None, target_id=user["id"] if user is not None else None,
           details=details, force=True)


def _known(username: str) -> dict[str, Any] | None:
    """The account a refused attempt was about, for the audit log (never a name no account has:
    that is often a password typed into the wrong field)."""
    return accounts.by_username(username) if signin.SIGN_IN_NAME.fullmatch(username) else None


def _refuse(username: str, next_url: str, key: str, status: int, **values: Any):
    auth.flash_t(key, "error", **values)
    return render_template(TEMPLATE, next_url=next_url, username=username), status


@bp.route("/login/recovery", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def login_recovery():
    next_url = request.values.get("next", "")
    if signin_modes.current() != signin_modes.PLATFORM:
        return redirect(url_for("auth.login", next=next_url or None))
    if request.method == "GET":
        if auth.current_user() is not None:
            return redirect(safe_next("/", next_url))
        return render_template(TEMPLATE, next_url=next_url, username="")
    username = (request.form.get("username") or "").strip()[:50]
    password = request.form.get("password") or ""
    if not SqlLimiter(db.session).hit(client_network(), "login:recovery-ip", MAX_PER_IP, WINDOW_SECONDS):
        _audit("user.recovery_sign_in_failed", _known(username), reason="too_many_attempts")
        return _refuse(username, next_url, "auth.error.too_many_attempts", 429)
    try:
        user = signin.verify(username, password, eligible=auth.is_admin)
    except signin.Refused as refusal:
        known = _known(username)
        if refusal.status == 429:
            reason = "too_many_attempts"
        elif known is not None and not auth.is_admin(known):
            reason = "not_admin"
        else:
            reason = "wrong_password" if known is not None else "unknown_account"
        _audit("user.recovery_sign_in_failed", known, reason=reason)
        current_app.logger.warning("Refused recovery sign-in (%s) from %s", reason, client_ip())
        return _refuse(username, next_url, refusal.key, refusal.status, **refusal.values)
    _audit("user.recovery_sign_in", user, two_step=two_step.enabled(user["id"]))
    current_app.logger.warning("Recovery sign-in of administrator %r from %s", user["username"], client_ip())
    default = url_for("admin.home") if "admin.home" in current_app.view_functions else "/"
    return signin.finish(user, remember=False, next_url=next_url, method="recovery", default=default)
