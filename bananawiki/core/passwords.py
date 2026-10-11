"""Password hashing (Werkzeug's scrypt, falling back to PBKDF2) and the policy for new passwords.

A new password (:func:`check_new_password`) must be 8 to 1024 characters
long, must not contain the user name or the part of the email address before
the ``@`` (parts shorter than 3 characters are not looked for), and must not
be one of the 10,000 most common passwords (``data/common-passwords.txt``,
compared without case and surrounding spaces). Existing passwords are never
checked again: signing in only verifies the hash.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources

from werkzeug.security import check_password_hash as _check
from werkzeug.security import generate_password_hash as _generate

MIN_LENGTH = 8
MAX_LENGTH = 1024
PERSONAL_MIN_LENGTH = 3  # shorter names and address parts are not looked for inside passwords
COMMON_PASSWORDS = "data/common-passwords.txt"


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


@lru_cache(maxsize=8)
def _dummy_hash(method: str | None = None) -> str:
    return hash_password("bananawiki-timing-equaliser", method)


def _parameters(stored_hash: str) -> str:
    """The method and cost part of a Werkzeug hash (``scrypt:32768:8:1``)."""
    return stored_hash.split("$", 1)[0]


def burn_time(password: str, method: str | None = None) -> None:
    """Spend the same time as a real check, for unknown usernames.

    Pass the configured hash *method*: a dummy hashed any other way would
    take measurably more or less time than the real accounts' hashes.
    """
    verify_password(_dummy_hash(method), password or "")


def needs_rehash(stored_hash: str | None, method: str | None = None) -> bool:
    """True when *stored_hash* was made with another method or cost than *method* gives.

    Hashes carried over from older versions (or from before an operator
    changed ``BW_PASSWORD_HASH_METHOD``) are upgraded at the next sign-in.
    """
    if not stored_hash or "$" not in stored_hash:
        return False
    return _parameters(stored_hash) != _parameters(_dummy_hash(method))


def validate_password(password: str | None) -> str | None:
    """Return an i18n error key when *password* is unacceptable, else None."""
    if not password or len(password) < MIN_LENGTH:
        return "auth.error.password_too_short"
    if len(password) > MAX_LENGTH:
        return "auth.error.password_too_long"
    return None


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    """The bundled list of common passwords (lower case); ``#`` lines are comments."""
    text = resources.files(__package__).joinpath(COMMON_PASSWORDS).read_text(encoding="utf-8")
    return frozenset(line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#"))


def is_common(password: str) -> bool:
    return password.strip().lower() in common_passwords()


def _personal_parts(username: str | None, email: str | None) -> set[str]:
    """The user name and the address before the ``@`` (also without a ``+tag``), lower case."""
    local = (email or "").strip().lower().rpartition("@")[0]
    parts = {(username or "").strip().lower(), local, local.split("+", 1)[0]}
    return {part for part in parts if len(part) >= PERSONAL_MIN_LENGTH}


def check_new_password(password: str | None, *, username: str | None = None,
                       email: str | None = None) -> str | None:
    """Return an i18n error key when *password* may not be chosen as a new password, else None.

    Only for passwords being set (sign-up, changes, resets, administrators
    setting one): accounts that already have a weaker password keep signing in.
    """
    problem = validate_password(password)
    if problem or not password:
        return problem
    lowered = password.lower()
    if any(part in lowered for part in _personal_parts(username, email)):
        return "auth.error.password_personal"
    if is_common(password):
        return "auth.error.password_common"
    return None
