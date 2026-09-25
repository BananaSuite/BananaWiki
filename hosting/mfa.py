"""TOTP and one-time recovery codes for hosting portal accounts."""

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

from . import config
from .db import get_hosting_db_context


def _fernet():
    raw = hashlib.sha256(
        (config.HOSTING_SECRET_KEY + "\0hosting-mfa-v1").encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(token):
    try:
        return _fernet().decrypt((token or "").encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def _decode_secret(secret):
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(padded, casefold=True)


def totp_code(secret, when=None):
    counter = int((time.time() if when is None else when) // 30)
    digest = hmac.new(
        _decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_counter(secret, code, when=None, window=1):
    code = str(code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isascii() or not code.isdigit() or not secret:
        return None
    now = time.time() if when is None else float(when)
    try:
        for offset in range(-int(window), int(window) + 1):
            moment = now + offset * 30
            if moment >= 0 and hmac.compare_digest(totp_code(secret, moment), code):
                return int(moment // 30)
    except (ValueError, TypeError):
        pass
    return None


def verify_totp(secret, code, when=None, window=1):
    return matching_counter(secret, code, when, window) is not None


def consume_totp(account_id, code):
    """Accept each authenticator time step once, including concurrent logins."""
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT totp_enabled,totp_secret_encrypted,totp_last_counter FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        counter = matching_counter(decrypt_secret(row[1]), code) if row and row[0] else None
        if counter is None or counter <= row[2]:
            return False
        conn.execute("UPDATE accounts SET totp_last_counter=? WHERE id=?", (counter, account_id))
        conn.commit()
        return True


def provisioning_uri(secret, username):
    issuer = "BananaWiki Hosting"
    label = quote(f"{issuer}:{username}", safe="")
    return (
        f"otpauth://totp/{label}?secret={quote(secret)}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


def qr_data_uri(uri):
    import qrcode
    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def generate_recovery_codes(count=10):
    return [
        f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}-"
        f"{secrets.token_hex(2).upper()}"
        for _ in range(count)
    ]


def _recovery_hash(code):
    normalized = str(code or "").replace("-", "").replace(" ", "").upper()
    return hmac.new(
        config.HOSTING_SECRET_KEY.encode("utf-8"),
        ("mfa-recovery-v1\0" + normalized).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def recovery_hashes(codes):
    return json.dumps([_recovery_hash(code) for code in codes])


def consume_recovery_code(account_id, code):
    candidate = _recovery_hash(code)
    with get_hosting_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT totp_recovery_hashes FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        try:
            hashes = json.loads(row[0] if row else "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            hashes = []
        match = next(
            (stored for stored in hashes if hmac.compare_digest(stored, candidate)),
            None,
        )
        if match is None:
            conn.rollback()
            return False
        hashes.remove(match)
        conn.execute(
            "UPDATE accounts SET totp_recovery_hashes=? WHERE id=?",
            (json.dumps(hashes), account_id),
        )
        conn.commit()
        return True
