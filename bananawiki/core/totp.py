"""Authenticator-app codes (TOTP, RFC 6238) and one-time recovery codes.

Shared by the wiki's two-step verification and the hosting portal's. Each
application keeps its own storage and keys: authenticator secrets are
encrypted with a :func:`cipher` and recovery codes stored as
:func:`recovery_hash` digests, both derived from the application's secret
key and a purpose label, so a key or digest of one application is useless
to the other.

Codes are six digits for 30-second steps (SHA-1, what every authenticator
app accepts by default); one step either side is accepted for clock drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

STEP_SECONDS = 30
RECOVERY_CODES = 10


def cipher(secret_key: str, purpose: str) -> Fernet:
    """The key that encrypts authenticator secrets: SHA-256 of *secret_key*, a NUL and *purpose*."""
    raw = hashlib.sha256((secret_key + "\0" + purpose).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(fernet: Fernet, secret: str) -> str:
    return fernet.encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(fernet: Fernet, token: str | None) -> str:
    """The secret, or ``""`` when *token* does not decrypt (another key, damaged data)."""
    try:
        return fernet.decrypt((token or "").encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def generate_secret() -> str:
    """A new 160-bit secret in base 32 without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, when: float | None = None) -> str:
    counter = int((time.time() if when is None else when) // STEP_SECONDS)
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    digest = hmac.new(base64.b32decode(padded, casefold=True), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_counter(secret: str, code: str, when: float | None = None, window: int = 1) -> int | None:
    """The time step whose code *code* is (within *window* steps of now), or None."""
    code = str(code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isascii() or not code.isdigit() or not secret:
        return None
    now = time.time() if when is None else float(when)
    try:
        for offset in range(-window, window + 1):
            moment = now + offset * STEP_SECONDS
            if moment >= 0 and hmac.compare_digest(totp_code(secret, moment), code):
                return int(moment // STEP_SECONDS)
    except (ValueError, TypeError):
        return None
    return None


def generate_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """Codes such as ``3F2A-9C01-B7E4`` (48 random bits each)."""
    return ["-".join(secrets.token_hex(2).upper() for _ in range(3)) for _ in range(count)]


def normalize_recovery_code(code: str | None) -> str:
    return str(code or "").replace("-", "").replace(" ", "").upper()


def recovery_hash(secret_key: str, code: str | None, label: str) -> str:
    """HMAC-SHA256 (hex) of a normalised recovery code, keyed by *secret_key* and bound to *label*."""
    message = (label + "\0" + normalize_recovery_code(code)).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def provisioning_uri(issuer: str, account: str, secret: str) -> str:
    """The ``otpauth://`` link authenticator apps read from the QR code.

    Apps split the label on its first colon, so a colon in either name becomes a space (labels only:
    codes depend on the secret alone).
    """
    issuer, account = (" ".join(name.replace(":", " ").split()) for name in (issuer, account))
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={quote(secret)}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def qr_data_uri(uri: str) -> str:
    """*uri* as a QR code PNG in a ``data:`` URL."""
    import qrcode

    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
