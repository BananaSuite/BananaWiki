"""Secrets derived from the instance key: encrypted settings and token digests.

Every function here is keyed by the instance ``SECRET_KEY`` and produces output
that is byte-compatible with BananaWiki 1.4, so an upgraded installation keeps
its encrypted settings and API tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTED_PREFIX = "fernet:"
_FERNET_SALT = b"BananaWiki-DB-Encryption-v1"
_FERNET_ITERATIONS = 100_000

# Labels used by 1.4 to derive one HMAC key per token family. Kept verbatim.
TOKEN_LABEL_API = b"BW-API-SERVICE-HMAC:"
TOKEN_LABEL_LEGACY_API = b"BW-API-TOKEN-HMAC:"
TOKEN_LABEL_USERBOT = b"BW-USERBOT-TOKEN-HMAC:"


@lru_cache(maxsize=8)
def _fernet(secret_key: str) -> Fernet:
    raw = hashlib.pbkdf2_hmac("sha256", secret_key.encode("utf-8"), _FERNET_SALT, _FERNET_ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(secret_key: str, plaintext: str | None) -> str | None:
    """Encrypt a value for storage. Empty values are stored as they are."""
    if not plaintext:
        return plaintext
    return ENCRYPTED_PREFIX + _fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(secret_key: str, stored: str | None) -> str:
    """Decrypt a stored value.

    Values without the ``fernet:`` prefix are legacy plaintext and returned
    as they are. A value that no longer decrypts (the key changed) reads as
    an empty string so the operator is prompted to enter it again.
    """
    if not stored:
        return ""
    if not stored.startswith(ENCRYPTED_PREFIX):
        return stored
    try:
        return _fernet(secret_key).decrypt(stored[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(ENCRYPTED_PREFIX)


def token_digest(secret_key: str, label: bytes, token: str) -> str:
    """HMAC-SHA256 (hex) of an API token, keyed per token family."""
    key = hashlib.sha256(label + secret_key.encode("utf-8")).digest()
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive(secret_key: str, purpose: str) -> str:
    """Derive a stable, purpose-bound secret (hex) from the instance key."""
    return hmac.new(secret_key.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(left: str | bytes, right: str | bytes) -> bool:
    if isinstance(left, str):
        left = left.encode("utf-8")
    if isinstance(right, str):
        right = right.encode("utf-8")
    return hmac.compare_digest(left, right)
