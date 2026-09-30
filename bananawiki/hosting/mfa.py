"""Authenticator (TOTP) second factor and one-time recovery codes.

Byte-compatible with 1.4 so enrolled accounts keep working: the TOTP secret
is Fernet-encrypted with ``sha256(HOSTING_SECRET_KEY + "\\0hosting-mfa-v1")``,
recovery codes are stored as HMAC-SHA256 (key ``HOSTING_SECRET_KEY``,
message ``"mfa-recovery-v1\\0" + normalised code``) in a JSON list, and each
30-second step is accepted once (``totp_last_counter``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from ..core.timeutil import now_sql
from .db import db

ISSUER = "BananaWiki Hosting"
RECOVERY_CODES = 10


def _key() -> str:
    return current_app.config["HOSTING"].secret_key


def _fernet() -> Fernet:
    raw = hashlib.sha256((_key() + "\0hosting-mfa-v1").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    try:
        return _fernet().decrypt((token or "").encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def totp_code(secret: str, when: float | None = None) -> str:
    counter = int((time.time() if when is None else when) // 30)
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    digest = hmac.new(base64.b32decode(padded, casefold=True), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_counter(secret: str, code: str, when: float | None = None, window: int = 1) -> int | None:
    code = str(code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isascii() or not code.isdigit() or not secret:
        return None
    now = time.time() if when is None else float(when)
    try:
        for offset in range(-window, window + 1):
            moment = now + offset * 30
            if moment >= 0 and hmac.compare_digest(totp_code(secret, moment), code):
                return int(moment // 30)
    except (ValueError, TypeError):
        return None
    return None


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
    return ["-".join(secrets.token_hex(2).upper() for _ in range(3)) for _ in range(count)]


def recovery_hash(code: str) -> str:
    normalized = str(code or "").replace("-", "").replace(" ", "").upper()
    return hmac.new(_key().encode("utf-8"), ("mfa-recovery-v1\0" + normalized).encode("utf-8"),
                    hashlib.sha256).hexdigest()


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
    """Turn on two-step sign-in; returns the recovery codes (shown once) or None if already on."""
    codes = generate_recovery_codes()
    changed = db.execute(
        "UPDATE accounts SET totp_enabled = 1, totp_secret_encrypted = ?, totp_recovery_hashes = ?, "
        "totp_last_counter = ?, totp_enabled_at = ?, session_version = session_version + 1 "
        "WHERE id = ? AND totp_enabled = 0",
        (encrypt_secret(secret), json.dumps([recovery_hash(c) for c in codes]), counter, now_sql(), account_id),
    ).rowcount
    return codes if changed else None


def disable(account_id: str) -> None:
    db.execute(
        "UPDATE accounts SET totp_enabled = 0, totp_secret_encrypted = '', totp_recovery_hashes = '[]', "
        "totp_last_counter = -1, totp_enabled_at = NULL, session_version = session_version + 1 WHERE id = ?",
        (account_id,),
    )


def provisioning_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}", safe="")
    return f"otpauth://totp/{label}?secret={quote(secret)}&issuer={quote(ISSUER)}&algorithm=SHA1&digits=6&period=30"


def qr_data_uri(uri: str) -> str:
    import qrcode

    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
