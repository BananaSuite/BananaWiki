"""File validation, input validation, and URL safety helpers."""

import os
import re
from urllib.parse import urlparse, unquote

from flask import request

import config
from ._constants import _USERNAME_RE


def safe_unlink_in(folder, filename):
    """Defense-in-depth: unlink *filename* only if it resolves inside *folder*.

    User- and admin-facing routes that delete files referenced by a database
    column (avatar, attachment, favicon …) must guard against the database
    value containing a traversal payload: for example, an admin importing a
    tampered migration archive that sets ``avatar_filename`` to
    ``../../etc/passwd``.  Without this guard, subsequent administrative
    actions would unlink arbitrary filesystem paths.

    *filename* may legitimately contain a relative subdirectory (e.g.
    ``avatars/<uuid>.png``) but never an absolute path or a ``..`` segment.
    Returns True iff the file was successfully unlinked.
    """
    if not folder or not filename:
        return False
    # Reject absolute paths and any traversal segments.
    if os.path.isabs(filename):
        return False
    norm = filename.replace("\\", "/")
    if any(part == ".." for part in norm.split("/")):
        return False
    folder_abs = os.path.abspath(folder)
    target = os.path.abspath(os.path.join(folder_abs, filename))
    try:
        if os.path.commonpath([folder_abs, target]) != folder_abs:
            return False
    except ValueError:
        return False
    if not os.path.isfile(target):
        return False
    try:
        os.remove(target)
        return True
    except OSError:
        return False


# Extensions that are always blocked regardless of upload mode (security risk).
#
# Two categories are blocked here:
#
# 1. Native executables and Windows scripting extensions. These can be
#    executed if a user downloads and double-clicks them.
# 2. Extensions that browsers parse as same-origin active content
#    (``html``, ``htm``, ``xhtml``, ``svg``, ``xml``, ``mhtml``, ``mht``)
#    or that legacy server stacks may execute server-side
#    (``php``, ``phtml``, ``phar``, ``jsp``, ``jspx``, ``asp``, ``aspx``,
#    ``cgi``, ``pl``, ``rb``, ``sh``).  Even though BananaWiki itself does
#    not execute uploads, attachments live on the same origin as the wiki
#    and an HTML upload would let a contributor stage stored XSS.
#    ``py``, ``js``, ``ts``, and ``css`` are NOT blocked here because they
#    are in the default :data:`config.ATTACHMENT_ALLOWED_EXTENSIONS` for
#    code-snippet sharing on a wiki.
_ALWAYS_BLOCKED_EXTENSIONS = frozenset({
    # Native executables / scripting hosts
    "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "mst",
    "cpl", "hta", "inf", "ins", "isp", "jse", "lnk", "reg", "rgs",
    "sct", "shb", "shs", "vbe", "vbs", "wsc", "wsf", "wsh", "ws",
    "ps1", "ps2", "psc1", "psc2", "dll", "sys",
    # Browser-active content (XSS via same-origin attachment download).
    # Note: ``xml`` is intentionally NOT blocked here because it is in the
    # default :data:`config.ATTACHMENT_ALLOWED_EXTENSIONS` whitelist as a
    # legitimate document format.  ``download_attachment`` serves all page
    # attachments with ``Content-Disposition: attachment`` so the browser
    # will not render them inline.
    "html", "htm", "xhtml", "shtml", "xht",
    "svg", "svgz",
    "mhtml", "mht",
    "swf", "jar", "class",
    # Server-side scripting (defense-in-depth: wiki does not execute these,
    # but prevents accidental disasters if files are ever moved into a
    # different web root)
    "php", "php3", "php4", "php5", "php7", "phtml", "phar",
    "jsp", "jspx",
    "asp", "aspx", "ashx", "asmx",
    "cgi", "pl", "rb",
    "sh",
})


def _safe_ext(filename):
    """Extract and validate a file extension from *filename*.

    Returns the lowercased extension (without dot) if it contains only
    alphanumeric characters, or ``None`` if the extension is missing or
    contains unexpected characters.
    """
    if "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower()
    if not re.fullmatch(r"[a-z0-9]+", ext):
        return None
    return ext


def _parse_ext_list(text):
    """Parse a comma-separated extension list into a frozenset of lowercased extensions."""
    if not text or not text.strip():
        return frozenset()
    return frozenset(
        e.strip().lower().lstrip(".")
        for e in text.split(",")
        if e.strip()
    )


def _is_extension_allowed_by_settings(ext, settings):
    """Check whether *ext* is permitted by the current upload mode in *settings*.

    Returns True if the extension should be accepted, False if it should be
    rejected.  Falls back to the default allow-all mode when settings are
    unavailable.
    """
    if ext in _ALWAYS_BLOCKED_EXTENSIONS:
        return False

    platform_blacklist = _parse_ext_list(getattr(config, "PLATFORM_UPLOAD_BLACKLIST", ""))
    if settings is not None:
        platform_blacklist = platform_blacklist | _parse_ext_list(
            (settings or {}).get("platform_upload_blacklist", "")
        )
    if ext in platform_blacklist:
        return False

    mode = (settings or {}).get("upload_mode", "allow_all")

    if mode == "allow_all":
        return True

    if mode == "blacklist":
        blacklist = _parse_ext_list((settings or {}).get("upload_blacklist", ""))
        return ext not in blacklist

    # Default: whitelist mode
    custom_whitelist = _parse_ext_list((settings or {}).get("upload_whitelist", ""))
    if custom_whitelist:
        return ext in custom_whitelist
    # When no custom whitelist is set, fall back to config defaults
    return ext in config.ATTACHMENT_ALLOWED_EXTENSIONS


def allowed_file(filename):
    """Return True if *filename* has an extension permitted for image uploads.

    Image uploads always use the hardcoded allowlist (no SVG for security).
    """
    ext = _safe_ext(filename)
    return ext is not None and ext in config.ALLOWED_EXTENSIONS


def allowed_attachment(filename, settings=None):
    """Return True if *filename* has an extension permitted for page attachments.

    When *settings* is provided (a dict from :func:`db.get_site_settings`), the
    upload mode (whitelist / blacklist / allow_all) is consulted.  If *settings*
    is ``None``, the default allow-all behavior is used.
    """
    ext = _safe_ext(filename)
    if ext is None:
        return False
    if ext in _ALWAYS_BLOCKED_EXTENSIONS:
        return False
    platform_blacklist = _parse_ext_list(getattr(config, "PLATFORM_UPLOAD_BLACKLIST", ""))
    if settings is not None:
        platform_blacklist = platform_blacklist | _parse_ext_list(
            (settings or {}).get("platform_upload_blacklist", "")
        )
    if ext in platform_blacklist:
        return False
    if settings is not None:
        return _is_extension_allowed_by_settings(ext, settings)
    return True


def get_effective_max_upload_size(settings=None):
    """Return the effective max upload size in bytes based on admin settings."""
    default_mb = 100
    if settings:
        raw_mb = settings.get("upload_max_size_mb")
        admin_mb = raw_mb if raw_mb is not None and raw_mb > 0 else default_mb
    else:
        admin_mb = default_mb
    return max(1, admin_mb) * 1024 * 1024


def allowed_chat_file(filename, settings=None):
    """Return True if *filename* has an extension permitted for chat attachments.

    Consults the admin upload mode (whitelist / blacklist / allow_all) from
    *settings* when provided; otherwise falls back to the default allow-all
    behavior.
    """
    ext = _safe_ext(filename)
    if ext is None:
        return False
    if ext in _ALWAYS_BLOCKED_EXTENSIONS:
        return False
    platform_blacklist = _parse_ext_list(getattr(config, "PLATFORM_UPLOAD_BLACKLIST", ""))
    if settings is not None:
        platform_blacklist = platform_blacklist | _parse_ext_list(
            (settings or {}).get("platform_upload_blacklist", "")
        )
    if ext in platform_blacklist:
        return False
    if settings and settings.get("upload_mode", "allow_all") != "whitelist":
        return _is_extension_allowed_by_settings(ext, settings)
    if settings and settings.get("upload_mode") == "whitelist":
        return ext in config.CHAT_ALLOWED_EXTENSIONS
    return True


def _is_valid_hex_color(value):
    """Return True if value is a valid 7-char hex color like #aabbcc."""
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value))


def _is_valid_username(value):
    """Return True if the username contains only safe characters.

    Allowed: letters, digits, underscores and hyphens.
    This prevents log-injection (newlines / control chars) and
    avoids confusing Unicode look-alikes.
    """
    return bool(_USERNAME_RE.fullmatch(value))


def _safe_referrer():
    """Return the path portion of request.referrer if it is same-origin; otherwise None.

    Only the *path* (plus query string) is returned so that the result is
    always a relative URL and can never redirect to an external host.
    """
    ref = request.referrer
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    # Build a safe relative URL from the path component only.
    safe = parsed.path or "/"
    if parsed.query:
        safe = f"{safe}?{parsed.query}"
    # Decode percent-encoded characters so that attacks like %2F%2Fevil.com
    # (which decodes to //evil.com) or %5Cevil.com (which decodes to \evil.com)
    # are caught by the same checks applied to the raw path.
    # Iterative decoding handles multi-encoded variants such as %252F%252F
    # (which decodes to %2F%2F on the first pass and then to // on the second).
    decoded_path = parsed.path or "/"
    while True:
        next_decoded = unquote(decoded_path)
        if next_decoded == decoded_path:
            break
        decoded_path = next_decoded
    # Reject protocol-relative, backslash-based, or encoded equivalents.
    if (
        not safe.startswith("/")
        or safe.startswith("//")
        or "\\" in safe
        or decoded_path.startswith("//")
        or "\\" in decoded_path
    ):
        return None
    return safe


def get_safe_next_url(target):
    """Validate *target* as a safe same-origin redirect URL.

    Returns the validated relative URL if safe; otherwise returns None.
    Accepts both a relative path (e.g. "/page/slug") and an absolute
    URL that must be same-origin.
    """
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.netloc and parsed.netloc != request.host:
        return None

    safe = parsed.path or "/"
    if parsed.query:
        safe = f"{safe}?{parsed.query}"

    # Decode and check for protocol-relative or backslash-based attacks
    decoded_path = parsed.path or "/"
    while True:
        next_decoded = unquote(decoded_path)
        if next_decoded == decoded_path:
            break
        decoded_path = next_decoded

    if (
        not safe.startswith("/")
        or safe.startswith("//")
        or "\\" in safe
        or decoded_path.startswith("//")
        or "\\" in decoded_path
    ):
        return None

    return safe
