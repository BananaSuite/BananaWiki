"""Device cookies: a browser that signed in to an account before.

Such a browser has its own budget of failed passwords and wrong two-step codes
(:mod:`.signin`), so strangers cannot use up the owner's. A cookie is
``<account id>.<nonce>.<signature>``; the signature (with the wiki's secret)
covers the account's *device epoch* (``users.device_epoch``), a random value
that belongs to one password: ``users.device_epoch_stamp`` is a digest of the
password hash it was issued under.

* A new password, however it is set (account settings, administrators,
  ``bananawiki reset-password``, the hosting portal), no longer matches the
  stamp, so every device cookie of the account stops counting; the next
  sign-in starts a new epoch.
* Turning two-step verification on, off or resetting it clears the epoch
  (:func:`retire`): only browsers that pass the code under the current set-up
  are known devices for the code step, so cookies collected while it was off
  cannot multiply its limits.
* Re-hashing the same password with another method or cost carries the stamp
  over (:func:`carry_over`): the account's browsers stay known.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from flask import current_app, request
from werkzeug.wrappers import Response

from ...db import db

COOKIE_SUFFIX = "_device"
COOKIE_DAYS = 365
_NONCE_LENGTH = 22  # secrets.token_urlsafe(16)
_SIGNATURE_LENGTH = 32


def _cookie_secure() -> bool:
    return bool(current_app.session_interface.get_cookie_secure(current_app))


def cookie_name() -> str:
    # ``__Host-`` over HTTPS, as the session cookie: hosted wikis are sibling
    # subdomains and must not be able to plant cookies for each other.
    name = current_app.config["SESSION_COOKIE_NAME"] + COOKIE_SUFFIX
    return ("__Host-" + name) if _cookie_secure() else name


def password_stamp(password_hash: str | None) -> str:
    return hashlib.sha256(("bananawiki-device-epoch:" + (password_hash or "")).encode()).hexdigest()[:32]


def _epoch(user_id: Any) -> str | None:
    """The account's device epoch while it still belongs to its password, else None."""
    row = db.one("SELECT password, device_epoch, device_epoch_stamp FROM users WHERE id = ?", (str(user_id),))
    if row is None or not row["device_epoch"] or not hmac.compare_digest(
            str(row["device_epoch_stamp"] or ""), password_stamp(row["password"])):
        return None
    return row["device_epoch"]


def _signature(user_id: Any, epoch: str, nonce: str) -> str:
    message = f"device:{user_id}:{epoch}:{nonce}".encode()
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:_SIGNATURE_LENGTH]


def nonce(user: dict[str, Any]) -> str | None:
    """The nonce of this browser's device cookie when it is valid for *user*."""
    owner, _, rest = (request.cookies.get(cookie_name()) or "").partition(".")
    value, _, signature = rest.partition(".")
    if (owner != str(user["id"]) or len(value) != _NONCE_LENGTH or len(signature) != _SIGNATURE_LENGTH
            or not value.isascii() or not signature.isascii()):
        return None
    epoch = _epoch(user["id"])
    if epoch is None:
        return None
    return value if hmac.compare_digest(signature, _signature(user["id"], epoch, value)) else None


def _issue_epoch(user: dict[str, Any]) -> str | None:
    """The epoch to sign a new cookie with: the current one, or a new one for a new password."""
    with db.transaction():
        epoch = _epoch(user["id"])
        if epoch is not None:
            return epoch
        epoch = secrets.token_urlsafe(16)
        # Only for the password the sign-in was checked against.
        started = db.execute("UPDATE users SET device_epoch = ?, device_epoch_stamp = ? WHERE id = ? AND password = ?",
                             (epoch, password_stamp(user["password"]), user["id"], user["password"])).rowcount
    return epoch if started else None


def set_cookie(response: Response, user: dict[str, Any]) -> None:
    """A fresh device cookie, after every sign-in and every change to the account's two-step set-up."""
    epoch = _issue_epoch(user)
    if epoch is None:
        return
    value = secrets.token_urlsafe(16)
    response.set_cookie(cookie_name(), f"{user['id']}.{value}.{_signature(user['id'], epoch, value)}",
                        max_age=COOKIE_DAYS * 86400, path="/", httponly=True, secure=_cookie_secure(),
                        samesite="Lax")


def retire(user_id: Any) -> None:
    """Every device cookie of the account stops counting (two-step verification changed)."""
    db.execute("UPDATE users SET device_epoch = NULL, device_epoch_stamp = NULL WHERE id = ?", (str(user_id),))


def carry_over(user_id: Any, old_hash: str, new_hash: str) -> None:
    """The same password was re-hashed: its device cookies keep counting (call in the re-hash's transaction)."""
    db.execute("UPDATE users SET device_epoch_stamp = ? WHERE id = ? AND device_epoch_stamp = ?",
               (password_stamp(new_hash), str(user_id), password_stamp(old_hash)))
