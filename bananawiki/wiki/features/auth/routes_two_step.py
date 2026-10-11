"""Routes: the code step of sign-in, the two-step settings and the reset by administrators.

/login/two-step (and POST /login/two-step/cancel), /settings/two-step,
POST /admin/users/<id>/two-step/reset.
See :mod:`.two_step` for the rules and :mod:`.signin` for the sign-in flow.
"""

from __future__ import annotations

import time
from typing import Any

from flask import abort, make_response, redirect, render_template, request, session, url_for

from ....core import totp
from ... import accounts, auth
from ..audit.service import record
from . import devices, signin, two_step
from .blueprint import SIGN_IN_GATES, bp

# The secret offered for set-up waits in the session (encrypted) this long.
ENROLMENT_KEY = "_two_step_enrolment"
ENROLMENT_SECONDS = 10 * 60
ACTIONS = ("enable", "disable", "renew")


# ── Sign-in ───────────────────────────────────────────────────────────────────


@bp.route("/login/two-step", methods=["GET", "POST"])
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def two_step_login():
    """The code after the password (or the hosting portal's sign-in)."""
    pending = signin.pending_second_step()
    if pending is None:
        session.pop(signin.TWO_STEP_PENDING_KEY, None)
        if auth.current_user() is not None:
            return redirect("/")
        auth.flash_t("auth.two_step.sign_in_again", "info")
        return redirect(url_for("auth.login"))
    user, state = pending
    if request.method == "POST":
        try:
            method = signin.check_code(user, request.form.get("code"))
        except signin.Refused as refusal:
            auth.flash_t(refusal.key, "error", **refusal.values)
            return render_template("auth/two_step_login.html"), refusal.status
        return signin.finish_second_step(user, state, method)
    return render_template("auth/two_step_login.html")


@bp.post("/login/two-step/cancel")
@auth.public
@auth.exempt(*SIGN_IN_GATES)
def cancel_two_step_login():
    """Leave the code step: the pending sign-in is forgotten, so this browser gives the password again
    before any code is taken (a plain link back to /login would keep the password step passed)."""
    session.pop(signin.TWO_STEP_PENDING_KEY, None)
    return redirect(url_for("auth.login"))


# ── Settings ──────────────────────────────────────────────────────────────────


def _stored_enrolment_secret() -> str:
    stored = session.get(ENROLMENT_KEY)
    if not isinstance(stored, dict):
        return ""
    try:
        age = time.time() - float(stored.get("at", 0))
    except (TypeError, ValueError):
        return ""
    return two_step.decrypt_secret(stored.get("secret")) if 0 <= age <= ENROLMENT_SECONDS else ""


def _enrolment_secret() -> str:
    """The secret to set up, the same while the person scans it (ten minutes)."""
    secret = _stored_enrolment_secret()
    if not secret:
        secret = totp.generate_secret()
        session[ENROLMENT_KEY] = {"secret": two_step.encrypt_secret(secret), "at": time.time()}
    return secret


def _show_codes(codes: list[str]):
    """The recovery codes, once: never cached, never shown again."""
    response = make_response(render_template("auth/two_step_codes.html", codes=codes))
    response.headers["Cache-Control"] = "no-store"
    return response


def _audit(action: str, user: dict[str, Any]) -> None:
    record(action, actor_id=user["id"], target_type="user", target_id=user["id"],
           details={"username": user["username"]})


def _enable(user: dict[str, Any], code: str):
    secret = _stored_enrolment_secret()
    if not secret:
        raise two_step.TwoStepError("auth.two_step.setup_expired")
    counter = totp.matching_counter(secret, code)
    if counter is None:
        raise two_step.TwoStepError("auth.two_step.invalid_code")
    codes = two_step.enable(user["id"], secret, counter)
    if codes is None:
        raise two_step.TwoStepError("auth.two_step.already_on")
    session.pop(ENROLMENT_KEY, None)
    # Sessions signed in with the password alone end; this one stays.
    auth.revoke_sessions(user["id"], except_session_id=auth.current_session_id())
    _audit("user.two_step_enabled", user)
    auth.flash_t("auth.two_step.enabled", "success")
    return _known_device(_show_codes(codes), user)


def _known_device(response, user: dict[str, Any]):
    """The change retired the account's device cookies; this browser just gave the password and a code."""
    devices.set_cookie(response, accounts.by_id(user["id"]) or user)
    return response


def _change(user: dict[str, Any], action: str):
    """Turn it on, off, or make new recovery codes. Every change takes the password."""
    from ..users.service import password_ok

    code = (request.form.get("code") or "")[:128]
    if not password_ok(user, request.form.get("password")):
        raise two_step.TwoStepError("auth.error.current_password_wrong")
    if action == "enable":
        return _enable(user, code)
    if not two_step.enabled(user["id"]):
        raise two_step.TwoStepError("auth.two_step.not_on")
    signin.check_code(user, code, sign_in=False)
    if action == "renew":
        codes = two_step.renew_codes(user["id"])
        if codes is None:
            raise two_step.TwoStepError("auth.two_step.not_on")
        _audit("user.two_step_codes_renewed", user)
        auth.flash_t("auth.two_step.renewed", "success")
        return _show_codes(codes)
    if not two_step.disable(user["id"]):
        raise two_step.TwoStepError("auth.two_step.not_on")
    auth.revoke_sessions(user["id"], except_session_id=auth.current_session_id())
    _audit("user.two_step_disabled", user)
    auth.flash_t("auth.two_step.disabled", "success")
    return _known_device(redirect(url_for("auth.two_step_settings")), user)


@bp.route("/settings/two-step", methods=["GET", "POST"])
@auth.exempt("account_steps")
def two_step_settings():
    """Set up, renew recovery codes or turn off; also where required administrators are sent."""
    from ..users.service import ProfileError

    if auth.is_impersonating():
        abort(403)
    user = auth.current_user()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action not in ACTIONS:
            abort(400)
        try:
            return _change(user, action)
        except (two_step.TwoStepError, ProfileError) as error:
            auth.flash_t(error.key, "error", **error.values)
        except signin.Refused as refusal:
            auth.flash_t(refusal.key, "error", **refusal.values)
        return redirect(url_for("auth.two_step_settings"))
    state = two_step.state(user["id"])
    secret = "" if state else _enrolment_secret()
    return render_template(
        "auth/two_step_settings.html", state=state, secret=secret, required=two_step.required(user),
        qr=two_step.provisioning_qr(secret, user["username"]) if secret else "",
    )


# ── Reset by an owner or superuser ────────────────────────────────────────────


@bp.post("/admin/users/<string:user_id>/two-step/reset")
@auth.admin_required
def two_step_reset(user_id: str):
    """For an account that lost its authenticator app and its recovery codes."""
    if auth.is_impersonating():
        abort(403)
    target = accounts.by_id(user_id)
    if target is None:
        abort(404)
    try:
        two_step.reset(auth.current_user(), target)
    except two_step.TwoStepError as error:
        auth.flash_t(error.key, "error", **error.values)
    else:
        auth.flash_t("auth.two_step.reset_done", "success", username=target["username"])
    return redirect(url_for("admin.user_detail", user_id=target["id"]))
