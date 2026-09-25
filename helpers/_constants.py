"""Constants shared across BananaWiki helpers."""

import re

import bleach

from helpers._passwords import generate_password_hash


ALLOWED_TAGS = list(bleach.ALLOWED_TAGS) + [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td",
    "ul", "ol", "li", "dl", "dt", "dd",
    "img", "figure", "figcaption",
    "div", "span", "section", "aside",
    "del", "ins", "sup", "sub",
]
ALLOWED_ATTRS = {
    "*": ["class", "id", "data-embed-type", "data-embed-slug", "data-embed-board"],
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height", "loading", "style"],
    "figure": ["style"],
    "div": ["style"],
    "td": ["align"],
    "th": ["align"],
}

# Lazy dummy hash for constant-time login checks.
# Computed on first call so Python 3.9 builds without hashlib.scrypt
# can still import this module (the hash is only needed at login time).
def _get_dummy_hash():
    """Lazily computed dummy hash for constant-time login guards."""
    if not hasattr(_get_dummy_hash, "_cache"):
        _get_dummy_hash._cache = generate_password_hash("dummy-constant-time-check")
    return _get_dummy_hash._cache

# Human-readable display labels for user roles.
ROLE_LABELS = {
    "user": "Member",
    "editor": "Editor",
    "admin": "Administrator",
    "owner": "Owner",
}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Maximum length for suspension reason text.
MAX_SUSPEND_REASON_LENGTH = 500

# Maximum allowed password length.  Bound exists purely as a DoS guard
# against attackers submitting megabyte-sized strings to scrypt; legitimate
# passwords are nowhere near this size.  Centralised here so login, signup,
# setup, and session-conflict-force all enforce the same value.
MAX_PASSWORD_LENGTH = 1024

# Minimum allowed password length.  Matches the value enforced in the UI
# helper text so server-side validation never silently disagrees.
MIN_PASSWORD_LENGTH = 8
