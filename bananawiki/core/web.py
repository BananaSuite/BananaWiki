"""Web security shared by the wiki and the hosting portal.

* CSRF: a per-session secret; forms send it as ``csrf_token``, scripts as the
  ``X-CSRF-Token`` header. Requests authenticated with a bearer token are
  exempt because browsers never attach those automatically.
* Content-Security-Policy with a per-request nonce. Templates must not use
  inline event handlers; inline ``<script>`` blocks need ``nonce="{{ csp_nonce }}"``.
* Standard hardening headers and safe redirect targets.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from flask import Flask, Response, abort, g, request, session

CSRF_SESSION_KEY = "_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADERS = ("X-CSRF-Token", "X-CSRFToken")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_valid() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    supplied = request.form.get(CSRF_FORM_FIELD) if request.form else None
    if not supplied:
        for header in CSRF_HEADERS:
            supplied = request.headers.get(header)
            if supplied:
                break
    if not supplied and request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            supplied = payload.get(CSRF_FORM_FIELD)
    if not supplied or not isinstance(supplied, str):
        return False
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        # JSON can carry an unpaired surrogate, which is not a valid token.
        return False


def csp_nonce() -> str:
    nonce = getattr(g, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(18)
        g._csp_nonce = nonce
    return nonce


@dataclass
class SecurityPolicy:
    """Headers applied to every HTML response."""

    frame_src: list[str] = field(default_factory=lambda: [
        "https://www.youtube-nocookie.com", "https://www.youtube.com", "https://player.vimeo.com",
    ])
    img_src: list[str] = field(default_factory=lambda: ["'self'", "data:", "blob:", "https:"])
    media_src: list[str] = field(default_factory=lambda: ["'self'", "blob:", "data:"])
    connect_src: list[str] = field(default_factory=lambda: ["'self'"])
    frame_ancestors: list[str] = field(default_factory=lambda: ["'self'"])
    extra_script_src: list[str] = field(default_factory=list)

    def header(self, nonce: str) -> str:
        directives = {
            "default-src": ["'self'"],
            "script-src": ["'self'", f"'nonce-{nonce}'", *self.extra_script_src],
            "style-src-elem": ["'self'", f"'nonce-{nonce}'"],
            # User content may carry sanitised spacing styles in style attributes.
            "style-src-attr": ["'unsafe-inline'"],
            "img-src": self.img_src,
            "media-src": self.media_src,
            "font-src": ["'self'", "data:"],
            "connect-src": self.connect_src,
            "frame-src": ["'self'", *self.frame_src],
            "frame-ancestors": self.frame_ancestors,
            "object-src": ["'none'"],
            "base-uri": ["'self'"],
            "form-action": ["'self'"],
            "manifest-src": ["'self'"],
            "worker-src": ["'self'", "blob:"],
        }
        return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def apply_security_headers(response: Response, policy: SecurityPolicy, *, hsts: bool) -> Response:
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    if "X-Frame-Options" not in headers and "'none'" in policy.frame_ancestors:
        headers["X-Frame-Options"] = "DENY"
    elif "X-Frame-Options" not in headers and policy.frame_ancestors == ["'self'"]:
        headers["X-Frame-Options"] = "SAMEORIGIN"
    if "Content-Security-Policy" not in headers:
        headers["Content-Security-Policy"] = policy.header(csp_nonce())
    if hsts and request.is_secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def is_safe_redirect(target: str | None) -> bool:
    """True for same-origin relative paths only."""
    if not target or not isinstance(target, str):
        return False
    if "\\" in target or any(ord(ch) < 32 for ch in target):
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    base = urlsplit(request.host_url)
    resolved = urlsplit(urljoin(request.host_url, target))
    return resolved.scheme == base.scheme and resolved.netloc == base.netloc


def safe_next(default: str, *candidates: str | None) -> str:
    for candidate in candidates:
        if is_safe_redirect(candidate):
            return candidate  # type: ignore[return-value]
    return default


def client_ip() -> str:
    """Remote address after ProxyFix (when enabled) has resolved it."""
    return (request.remote_addr or "unknown")[:64]


def install_csrf(app: Flask, *, exempt_prefixes: Iterable[str] = (), exempt_when=None) -> None:
    """Reject unsafe requests that lack a valid CSRF token."""
    prefixes = tuple(exempt_prefixes)

    @app.before_request
    def _csrf_protect():
        if request.method in SAFE_METHODS:
            return None
        if app.config.get("CSRF_DISABLED"):
            return None
        if request.endpoint and request.endpoint.endswith(".static"):
            return None
        if prefixes and request.path.startswith(prefixes):
            return None
        if exempt_when is not None and exempt_when():
            return None
        view = app.view_functions.get(request.endpoint or "")
        if view is not None and getattr(view, "_csrf_exempt", False):
            return None
        if not csrf_valid():
            abort(400, description="The form expired or was sent from another site. Reload the page and try again.")
        return None


def csrf_exempt(view):
    view._csrf_exempt = True
    return view
