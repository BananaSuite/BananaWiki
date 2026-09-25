"""Symmetric encryption utilities for sensitive database fields.

Uses Fernet (AES-128-CBC + HMAC-SHA256) to encrypt values at rest.
The encryption key is derived from the Flask secret key using PBKDF2,
so the same secret key is required to decrypt previously stored values.

Encrypted values are stored with a ``fernet:`` prefix so that legacy
plaintext values (from before encryption was introduced) can be
transparently detected and migrated on the next write.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

import config


#: Prefix prepended to every encrypted value so we can distinguish it from
#: legacy plaintext data during the migration period.
_ENCRYPTED_PREFIX = "fernet:"

#: Static salt for PBKDF2 key derivation.  This is *not* secret. Its purpose
#: is to domain-separate the derived key from the Flask secret key.
_KEY_DERIVATION_SALT = b"BananaWiki-DB-Encryption-v1"

#: PBKDF2 iterations.  The input key is already high-entropy (256-bit random),
#: so brute-force is infeasible regardless of the iteration count.  We still
#: use a healthy number for defence-in-depth.
_KEY_DERIVATION_ITERATIONS = 100_000

#: Columns in ``site_settings`` that hold sensitive data and should be
#: encrypted at rest.  Add column names here to extend encryption to
#: additional fields in the future.
#:
#: The two feedback columns belong to a plugin that no longer ships. They stay
#: listed because upgraded databases still have them, possibly holding a live
#: Telegram bot token, and dropping them here would stop that value being
#: treated as a secret.
ENCRYPTED_SETTINGS_COLUMNS = frozenset({
    "tts_gpu_auth_token",
    "feedback_bot_token",
    "feedback_telegram_userids",
})


# Cache the Fernet instance so PBKDF2 only runs once per process lifetime.
_cached_fernet: Fernet | None = None
_cached_key_input: str | None = None


def _get_fernet() -> Fernet:
    """Return a cached :class:`~cryptography.fernet.Fernet` instance.

    The Fernet key is derived from :pyattr:`config.SECRET_KEY` via PBKDF2.
    The result is cached so that the (intentionally expensive) key derivation
    runs only once per process.
    """
    global _cached_fernet, _cached_key_input

    current_key = config.SECRET_KEY
    if _cached_fernet is not None and _cached_key_input == current_key:
        return _cached_fernet

    raw = hashlib.pbkdf2_hmac(
        "sha256",
        current_key.encode("utf-8"),
        _KEY_DERIVATION_SALT,
        iterations=_KEY_DERIVATION_ITERATIONS,
        dklen=32,
    )
    _cached_fernet = Fernet(base64.urlsafe_b64encode(raw))
    _cached_key_input = current_key
    return _cached_fernet


def _reset_cache() -> None:
    """Clear the cached Fernet instance.

    This is primarily useful in tests that change :pyattr:`config.SECRET_KEY`
    at runtime and need the next encrypt/decrypt call to re-derive the key.
    """
    global _cached_fernet, _cached_key_input
    _cached_fernet = None
    _cached_key_input = None



def encrypt_value(plaintext: str | None) -> str | None:
    """Encrypt *plaintext* and return a prefixed Fernet token string.

    Empty or falsy values (including ``None``) are returned unchanged. There
    is nothing sensitive to protect.
    """
    if not plaintext:
        return plaintext
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _ENCRYPTED_PREFIX + token


def decrypt_value(stored: str | None) -> str | None:
    """Decrypt a value previously encrypted by :func:`encrypt_value`.

    * If *stored* carries the ``fernet:`` prefix the encrypted payload is
      decrypted and the plaintext returned.
    * If decryption fails (e.g. the secret key changed after a site import),
      an empty string is returned so the caller gets a safe default rather
      than garbled ciphertext.
    * If *stored* does **not** have the prefix it is assumed to be a legacy
      plaintext value and returned as-is.
    * ``None`` and empty strings are returned unchanged.
    """
    if not stored:
        return stored
    if not stored.startswith(_ENCRYPTED_PREFIX):
        # Legacy plaintext: return as-is; it will be encrypted on next write.
        return stored
    encrypted_part = stored[len(_ENCRYPTED_PREFIX):]
    try:
        f = _get_fernet()
        return f.decrypt(encrypted_part.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Key mismatch (e.g. different secret key after import): return
        # empty string rather than the unreadable ciphertext.
        return ""
