"""Password hashing with Werkzeug's formats (scrypt, falling back to PBKDF2)."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from werkzeug.security import check_password_hash as _check
from werkzeug.security import generate_password_hash as _generate

MIN_LENGTH = 8
MAX_LENGTH = 1024


@lru_cache(maxsize=1)
def scrypt_available() -> bool:
    scrypt = getattr(hashlib, "scrypt", None)
    if scrypt is None:
        return False
    try:
        scrypt(b"probe", salt=b"salt", n=2, r=1, p=1, dklen=8)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return True


def resolve_method(configured: str | None) -> str:
    method = (configured or "auto").strip() or "auto"
    if method == "pbkdf2":
        return "pbkdf2:sha256"
    if method == "auto":
        return "scrypt" if scrypt_available() else "pbkdf2:sha256"
    return method


def hash_password(password: str, method: str | None = None) -> str:
    return _generate(password, method=resolve_method(method), salt_length=16)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Check *password*; unknown or malformed hashes simply fail."""
    if not stored_hash or password is None:
        return False
    try:
        return _check(stored_hash, password)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password("bananawiki-timing-equaliser")


def burn_time(password: str) -> None:
    """Spend the same time as a real check, for unknown usernames."""
    verify_password(_dummy_hash(), password or "")


def validate_password(password: str | None) -> str | None:
    """Return an i18n error key when *password* is unacceptable, else None."""
    if not password or len(password) < MIN_LENGTH:
        return "auth.error.password_too_short"
    if len(password) > MAX_LENGTH:
        return "auth.error.password_too_long"
    return None
