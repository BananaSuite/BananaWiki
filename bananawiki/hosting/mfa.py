"""Authenticator (TOTP) second factor and one-time recovery codes.

Byte-compatible with 1.4 so enrolled accounts keep working: the TOTP secret
is Fernet-encrypted with ``sha256(HOSTING_SECRET_KEY + "\\0hosting-mfa-v1")``,
recovery codes are stored as HMAC-SHA256 (key ``HOSTING_SECRET_KEY``,
message ``"mfa-recovery-v1\\0" + normalised code``) in a JSON list, and each
30-second step is accepted once (``totp_last_counter``). The code arithmetic
is shared with the wiki (:mod:`bananawiki.core.totp`).
"""

from __future__ import annotations

import hmac
import json

from cryptography.fernet import Fernet
from flask import current_app

from ..core import totp
from ..core.timeutil import now_sql
from ..core.totp import generate_secret, matching_counter, totp_code  # noqa: F401 - part of this module's API
from .db import db

RECOVERY_CODES = totp.RECOVERY_CODES


def _key() -> str:
    return current_app.config["HOSTING"].secret_key


def _fernet() -> Fernet:
    return totp.cipher(_key(), "hosting-mfa-v1")


def encrypt_secret(secret: str) -> str:
    return totp.encrypt_secret(_fernet(), secret)


def decrypt_secret(token: str | None) -> str:
    return totp.decrypt_secret(_fernet(), token)


def consume_totp(account_id: str, code: str) -> bool:
    """Accept a code once per time step, even for concurrent sign-ins."""
    with db.transaction():
        row = db.one("SELECT totp_enabled, totp_secret_encrypted, totp_last_counter FROM accounts WHERE id = ?",
                     (account_id,))
        if not row or not row["totp_enabled"]:
            return False
        counter = matching_counter(decrypt_secret(row["totp_secret_encrypted"]), code)
        if counter is None or counter <= int(row["totp_last_counter"]):
            return False
        db.execute("UPDATE accounts SET totp_last_counter = ? WHERE id = ?", (counter, account_id))
        return True


def generate_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    return totp.generate_recovery_codes(count)


def recovery_hash(code: str) -> str:
    return totp.recovery_hash(_key(), code, "mfa-recovery-v1")


def consume_recovery_code(account_id: str, code: str) -> bool:
    candidate = recovery_hash(code)
    with db.transaction():
        stored = db.scalar("SELECT totp_recovery_hashes FROM accounts WHERE id = ?", (account_id,), default="[]")
        try:
            hashes = [h for h in json.loads(stored or "[]") if isinstance(h, str)]
        except ValueError:
            hashes = []
        match = next((h for h in hashes if hmac.compare_digest(h, candidate)), None)
        if match is None:
            return False
        hashes.remove(match)
        db.execute("UPDATE accounts SET totp_recovery_hashes = ? WHERE id = ?", (json.dumps(hashes), account_id))
        return True


def verify_second_factor(account_id: str, code: str) -> str | None:
    """Return ``"totp"`` or ``"recovery_code"`` when *code* is accepted."""
    code = (code or "")[:128]
    if consume_totp(account_id, code):
        return "totp"
    if consume_recovery_code(account_id, code):
        return "recovery_code"
    return None


def enable(account_id: str, secret: str, counter: int) -> list[str] | None:
    """Turn on two-step sign-in; returns the recovery codes (shown once) or None if already on.

    Like :func:`disable`, it ends every session and bumps ``mfa_epoch``,
    which retires every device cookie (see :mod:`.auth`).
    """
    codes = generate_recovery_codes()
    changed = db.execute(
        "UPDATE accounts SET totp_enabled = 1, totp_secret_encrypted = ?, totp_recovery_hashes = ?, "
        "totp_last_counter = ?, totp_enabled_at = ?, session_version = session_version + 1, "
        "mfa_epoch = mfa_epoch + 1 WHERE id = ? AND totp_enabled = 0",
        (encrypt_secret(secret), json.dumps([recovery_hash(c) for c in codes]), counter, now_sql(), account_id),
    ).rowcount
    return codes if changed else None


def disable(account_id: str) -> None:
    db.execute(
        "UPDATE accounts SET totp_enabled = 0, totp_secret_encrypted = '', totp_recovery_hashes = '[]', "
        "totp_last_counter = -1, totp_enabled_at = NULL, session_version = session_version + 1, "
        "mfa_epoch = mfa_epoch + 1 WHERE id = ?",
        (account_id,),
    )


def provisioning_uri(secret: str, username: str) -> str:
    """The authenticator link; the issuer (``HOSTING_SERVICE_NAME``) only labels the entry in the app, so
    renaming the platform leaves enrolled authenticators working (codes depend on the secret alone)."""
    return totp.provisioning_uri(current_app.config["HOSTING"].service_name, username, secret)


def qr_data_uri(uri: str) -> str:
    return totp.qr_data_uri(uri)
