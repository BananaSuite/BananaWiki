"""Two-step verification: a code from an authenticator app after the password.

Every account may turn it on; owners and superusers who use it may require
it of administrators (``two_step_required_for_admins``), who are then sent to
set it up before anything else (:func:`pending_step`). Turning it on takes the
password and a code from the app, which proves the app was set up, and shows
ten one-time recovery codes once. Turning it off and making new recovery
codes take the password and a code. Owners and superusers can reset it for
an account they may manage that lost both (``reset``); ``bananawiki
reset-two-step`` does it from the server for anyone.

Storage (``auth__two_step``, schema 9; a row means it is on): the secret is
encrypted with a key derived from the wiki's secret key, recovery codes are
kept as keyed digests, and ``last_counter`` is the last 30-second step
accepted, so a code works once even for concurrent sign-ins. Changing the
secret key makes both unreadable; those accounts then need a reset.

The sign-in step and its attempt limits are in :mod:`.signin`; the pages in
:mod:`.routes_two_step`. Turning it on, off or resetting it retires the
account's device cookies (:mod:`.devices`), so only browsers that passed a
code under the current set-up keep budgets of their own. API tokens and
impersonation do not involve it.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from cryptography.fernet import Fernet
from flask import current_app

from ....core import totp
from ....core.timeutil import now_sql
from ... import accounts, auth, settings
from ...db import db
from ..audit.service import record
from . import devices

TABLE = "auth__two_step"
REQUIRED_SETTING = "two_step_required_for_admins"
_CIPHER_PURPOSE = "wiki-two-step-v1"
_RECOVERY_LABEL = "wiki-two-step-recovery-v1"


class TwoStepError(ValueError):
    """A refused change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


def _secret_key() -> str:
    return current_app.config["BW"].secret_key


def _fernet(secret_key: str | None = None) -> Fernet:
    return totp.cipher(secret_key or _secret_key(), _CIPHER_PURPOSE)


def encrypt_secret(secret: str) -> str:
    return totp.encrypt_secret(_fernet(), secret)


def decrypt_secret(token: str | None, secret_key: str | None = None) -> str:
    """The authenticator secret, or ``""`` when this wiki cannot read it.

    *secret_key* defaults to the running wiki's; a hosted wiki's tenant tasks
    run without the application and pass their own.
    """
    return totp.decrypt_secret(_fernet(secret_key), token)


def _recovery_hash(code: str | None) -> str:
    return totp.recovery_hash(_secret_key(), code, _RECOVERY_LABEL)


def _hashes(stored: str | None) -> list[str]:
    try:
        values = json.loads(stored or "[]")
    except ValueError:
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


# State


def state(user_id: Any) -> dict[str, Any] | None:
    """``{"enabled_at", "codes_left"}`` while two-step verification is on for the account, else None."""
    row = db.one(f"SELECT enabled_at, recovery_hashes FROM {TABLE} WHERE user_id = ?", (str(user_id),))
    if row is None:
        return None
    return {"enabled_at": row["enabled_at"], "codes_left": len(_hashes(row["recovery_hashes"]))}


def enabled(user_id: Any) -> bool:
    return bool(db.scalar(f"SELECT 1 FROM {TABLE} WHERE user_id = ?", (str(user_id),)))


def required(user: dict[str, Any] | None) -> bool:
    """Whether this wiki requires two-step verification of *user* (administrators, when switched on)."""
    return bool(user) and bool(settings.get(REQUIRED_SETTING)) and auth.is_admin(user)


def missing(user: dict[str, Any] | None) -> bool:
    """Required of *user* but not turned on yet."""
    return required(user) and not enabled(user["id"])  # type: ignore[index]


def pending_step(user: dict[str, Any], **_: Any) -> str | None:
    """``account.pending_step`` interceptor: administrators who must turn it on first."""
    return "auth.two_step_settings" if missing(user) else None


def fingerprint(user: dict[str, Any]) -> str:
    """Changes when the password changes or two-step verification is turned off, reset or on again."""
    enabled_at = db.scalar(f"SELECT enabled_at FROM {TABLE} WHERE user_id = ?", (user["id"],), default="")
    message = f"two-step:{user['id']}:{user['password']}:{enabled_at}".encode()
    return hmac.new(_secret_key().encode(), message, "sha256").hexdigest()[:32]


# Codes


def _consume_totp(user_id: str, code: str) -> bool:
    """Accept an authenticator code once per 30-second step, even for concurrent attempts."""
    with db.transaction():
        row = db.one(f"SELECT secret_encrypted, last_counter FROM {TABLE} WHERE user_id = ?", (user_id,))
        if row is None:
            return False
        counter = totp.matching_counter(decrypt_secret(row["secret_encrypted"]), code)
        if counter is None or counter <= int(row["last_counter"]):
            return False
        db.execute(f"UPDATE {TABLE} SET last_counter = ? WHERE user_id = ?", (counter, user_id))
        return True


def _consume_recovery_code(user_id: str, code: str) -> bool:
    candidate = _recovery_hash(code)
    with db.transaction():
        stored = db.scalar(f"SELECT recovery_hashes FROM {TABLE} WHERE user_id = ?", (user_id,))
        hashes = _hashes(stored)
        match = next((value for value in hashes if hmac.compare_digest(value, candidate)), None)
        if match is None:
            return False
        hashes.remove(match)
        db.execute(f"UPDATE {TABLE} SET recovery_hashes = ? WHERE user_id = ?", (json.dumps(hashes), user_id))
        return True


def verify(user_id: Any, code: str | None) -> str | None:
    """``"totp"`` or ``"recovery_code"`` when *code* is accepted (and used up), else None.

    Callers count attempts (:func:`.signin.check_code`).
    """
    code = str(code or "").strip()[:128]
    if not code:
        return None
    if _consume_totp(str(user_id), code):
        return "totp"
    if _consume_recovery_code(str(user_id), code):
        return "recovery_code"
    return None


# Changes


def _new_codes() -> tuple[list[str], str]:
    codes = totp.generate_recovery_codes()
    return codes, json.dumps([_recovery_hash(code) for code in codes])


def enable(user_id: Any, secret: str, counter: int) -> list[str] | None:
    """Turn it on with *secret*, whose code for step *counter* was just entered.

    Returns the recovery codes to show once, or None when it was already on.
    """
    codes, hashes = _new_codes()
    with db.transaction():
        inserted = db.execute(
            f"INSERT INTO {TABLE} (user_id, secret_encrypted, recovery_hashes, last_counter, enabled_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
            (str(user_id), encrypt_secret(secret), hashes, counter, now_sql()),
        ).rowcount
        if inserted:
            # Browsers known from before have not passed a code: no budgets of their own.
            devices.retire(user_id)
    return codes if inserted else None


def renew_codes(user_id: Any) -> list[str] | None:
    """Replace the recovery codes; returns the new ones, or None when it is off."""
    codes, hashes = _new_codes()
    changed = db.execute(f"UPDATE {TABLE} SET recovery_hashes = ? WHERE user_id = ?", (hashes, str(user_id))).rowcount
    return codes if changed else None


def disable(user_id: Any) -> bool:
    """Turn it off (retiring the account's device cookies); False when it was not on."""
    with db.transaction():
        removed = db.execute(f"DELETE FROM {TABLE} WHERE user_id = ?", (str(user_id),)).rowcount == 1
        if removed:
            devices.retire(user_id)
    return removed


def reset(actor: dict[str, Any], target: dict[str, Any]) -> None:
    """An owner or superuser turns it off for an account that lost its app and codes.

    Only for an account *actor* may manage (the hierarchy of the administration
    pages), never *actor*'s own (that takes the password and a code). Every
    session of the account ends. Raises :class:`TwoStepError`.
    """
    from ..admin import service as admin_service

    with db.transaction():
        current_actor, current_target = accounts.by_id(actor["id"]), accounts.by_id(target["id"])
        if current_actor is None or current_target is None:
            raise TwoStepError("error.forbidden")
        if current_actor["id"] == current_target["id"]:
            raise TwoStepError("auth.two_step.reset_own")
        if (not admin_service.is_top(current_actor) or not auth.is_admin(current_actor)
                or auth.account_block(current_actor) or current_actor["password"] != actor["password"]):
            raise TwoStepError("auth.two_step.reset_top_only")
        protection = admin_service.protection_error(current_actor, current_target)
        if protection:
            raise TwoStepError(protection)
        if not disable(current_target["id"]):
            raise TwoStepError("auth.two_step.reset_not_on")
        auth.revoke_sessions(current_target["id"])
    record("user.two_step_reset", actor_id=current_actor["id"], target_type="user", target_id=current_target["id"],
           details={"username": current_target["username"]})


def provisioning_qr(secret: str, username: str) -> str:
    """The QR code (a ``data:`` URL) that adds *secret* to an authenticator app."""
    return totp.qr_data_uri(totp.provisioning_uri(settings.site_name(), username, secret))
