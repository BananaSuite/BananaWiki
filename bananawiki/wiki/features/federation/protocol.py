"""The federation wire protocol, version 1 (unchanged from BananaWiki 1.4, so 1.4 and 1.6 wikis pair).

A peer fetches ``GET /federation/v1/snapshot`` with four headers: its wiki id
(``BW-Wiki``), a Unix timestamp (``BW-Time``), a random 128-bit nonce
(``BW-Nonce``) and ``BW-Signature``, an HMAC-SHA256 with the shared pairing
key over ``BW-FED-1``, the method, the path, both wiki ids, the timestamp
and the nonce. The answer is a JSON snapshot whose body is signed the same
way (``BW-FED-1-RESPONSE``, bound to the request nonce). Timestamps outside
a two-minute window are refused and each nonce is accepted once.

Everything here is pure: no database, no network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

PATH = "/federation/v1/snapshot"
MAX_PEERS = 16
MAX_PAGES = 32
MAX_CONTENT = 128 * 1024
MAX_TITLE = 300
MAX_RESPONSE = 8 * 1024 * 1024
CLOCK_WINDOW = 120
POLL_SECONDS = 300
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_SOURCE_PATH = re.compile(r"/page/[A-Za-z0-9%_.~-]+")


class ProtocolError(ValueError):
    """Refused input; ``key`` is a translation key for administrators."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def wiki_id(value: Any) -> str:
    try:
        if isinstance(value, str) and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ProtocolError("federation.error.wiki_id")


def base_url(value: Any) -> str:
    """An HTTPS origin without credentials, path, query or fragment."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ProtocolError("federation.error.base_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ProtocolError("federation.error.base_url") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query
            or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ProtocolError("federation.error.base_url")
    return value.rstrip("/")


def pairing_secret(value: Any) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ProtocolError("federation.error.secret")
    return value


def new_secret() -> str:
    return secrets.token_hex(32)


def signature(secret: str, *parts: str) -> str:
    return hmac.new(bytes.fromhex(secret), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def request_headers(local_id: str, remote_id: str, secret: str, *, now: float | None = None,
                    nonce: str | None = None) -> dict[str, str]:
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    return {
        "BW-Wiki": local_id, "BW-Time": timestamp, "BW-Nonce": nonce,
        "BW-Signature": signature(secret, "BW-FED-1", "GET", PATH, local_id, remote_id, timestamp, nonce),
        "Accept": "application/json",
    }


def authenticate(headers: Any, local_id: str, peer_id: str, secret: str, *, now: float | None = None) -> str:
    """Check a request's signature and clock; return its nonce (the caller checks replay)."""
    timestamp, nonce = headers.get("BW-Time", ""), headers.get("BW-Nonce", "")
    supplied = headers.get("BW-Signature", "")
    if (not re.fullmatch(r"[0-9]{1,12}", timestamp) or not _NONCE.fullmatch(nonce)
            or not _HEX64.fullmatch(supplied) or not _HEX64.fullmatch(secret or "")
            or abs((time.time() if now is None else now) - int(timestamp)) > CLOCK_WINDOW):
        raise ProtocolError("federation.error.authentication")
    expected = signature(secret, "BW-FED-1", "GET", PATH, peer_id, local_id, timestamp, nonce)
    if headers.get("BW-Wiki") != peer_id or not hmac.compare_digest(expected, supplied):
        raise ProtocolError("federation.error.authentication")
    return nonce


def encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()


def response_signature(secret: str, source: str, recipient: str, nonce: str, body: bytes) -> str:
    return signature(secret, "BW-FED-1-RESPONSE", source, recipient, nonce, hashlib.sha256(body).hexdigest())


def verify_response(secret: str, source: str, recipient: str, nonce: str, body: bytes, supplied: str) -> None:
    expected = response_signature(secret, source, recipient, nonce, body)
    if not _HEX64.fullmatch(supplied or "") or not hmac.compare_digest(expected, supplied):
        raise ProtocolError("federation.error.response_signature")


def revision(title: str, content: str, source_path: str) -> str:
    return hashlib.sha256(encode([title, content, source_path])).hexdigest()


def page_fits(title: str, content: str) -> bool:
    return 1 <= len(title) <= MAX_TITLE and len(content.encode()) <= MAX_CONTENT


def validate_snapshot(payload: Any, source: str) -> dict[str, Any]:
    if (not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("wiki_id") != source
            or type(payload.get("sequence")) is not int or not 0 < payload["sequence"] < 2**63
            or not isinstance(payload.get("pages"), list) or len(payload["pages"]) > MAX_PAGES):
        raise ProtocolError("federation.error.snapshot")
    seen: set[str] = set()
    for page in payload["pages"]:
        if not isinstance(page, dict):
            raise ProtocolError("federation.error.snapshot")
        page_id = wiki_id(page.get("id"))
        title, content, path = (page.get(key) for key in ("title", "content", "source_path"))
        if (page_id in seen or not isinstance(title, str) or not isinstance(content, str)
                or not page_fits(title, content) or not isinstance(path, str) or len(path) > 2048
                or not _SOURCE_PATH.fullmatch(path) or page.get("revision") != revision(title, content, path)):
            raise ProtocolError("federation.error.snapshot")
        seen.add(page_id)
    return payload
