"""Version-one paired HTTPS snapshot protocol; no browser or ambient credentials."""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request

from http_transport import open_http

PATH = "/federation/v1/snapshot"
MAX_PEERS = 16
MAX_PAGES = 32
MAX_CONTENT = 128 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
CLOCK_WINDOW = 120
POLL_SECONDS = 300


def wiki_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Use the canonical wiki UUID shown by the other administrator.")
    return value


def base_url(value):
    parsed = urlsplit(value)
    if (len(value) > 512 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError("Use an HTTPS origin without credentials, a path, or a query.")
    # Validate the port now rather than deferring an invalid port until polling.
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid peer port.")
    return value.rstrip("/")


def pairing_secret(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Use a newly generated 64-character hexadecimal pairing key.")
    return value


def signature(secret, *parts):
    return hmac.new(bytes.fromhex(secret), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def request_headers(local_id, remote_id, secret, *, now=None, nonce=None):
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(16)
    return {
        "BW-Wiki": local_id, "BW-Time": timestamp, "BW-Nonce": nonce,
        "BW-Signature": signature(secret, "BW-FED-1", "GET", PATH,
                                  local_id, remote_id, timestamp, nonce),
        "Accept": "application/json",
    }


def authenticate(headers, local_id, peer, *, now=None):
    timestamp, nonce = headers.get("BW-Time", ""), headers.get("BW-Nonce", "")
    supplied = headers.get("BW-Signature", "")
    if (not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied)
            or abs((time.time() if now is None else now) - int(timestamp)) > CLOCK_WINDOW):
        raise ValueError("Invalid federation authentication.")
    expected = signature(peer["secret"], "BW-FED-1", "GET", PATH,
                         peer["wiki_id"], local_id, timestamp, nonce)
    if headers.get("BW-Wiki") != peer["wiki_id"] or not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid federation authentication.")
    return nonce


def encode(payload):
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()


def response_signature(secret, source, recipient, nonce, body):
    return signature(secret, "BW-FED-1-RESPONSE", source, recipient, nonce,
                     hashlib.sha256(body).hexdigest())


def revision(title, content, source_path):
    return hashlib.sha256(encode([title, content, source_path])).hexdigest()


def validate_snapshot(payload, source):
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or payload.get("wiki_id") != source
            or type(payload.get("sequence")) is not int
            or not 0 < payload["sequence"] < 2**63
            or not isinstance(payload.get("pages"), list)
            or len(payload["pages"]) > MAX_PAGES):
        raise ValueError("Invalid federation snapshot.")
    seen = set()
    for page in payload["pages"]:
        if not isinstance(page, dict):
            raise ValueError("Invalid shared page.")
        page_id = wiki_id(page.get("id"))
        title, content, path = (page.get(key) for key in ("title", "content", "source_path"))
        if (page_id in seen or not isinstance(title, str) or not 1 <= len(title) <= 300
                or not isinstance(content, str) or len(content.encode()) > MAX_CONTENT
                or not isinstance(path, str) or len(path) > 2048
                or not re.fullmatch(r"/page/[A-Za-z0-9%_.~-]+", path)
                or page.get("revision") != revision(title, content, path)):
            raise ValueError("Invalid shared page.")
        seen.add(page_id)
    return payload


def fetch(local_id, peer):
    headers = request_headers(local_id, peer["wiki_id"], peer["secret"])
    request = Request(base_url(peer["base_url"]) + PATH, headers=headers)
    with open_http(request, timeout=10, total_timeout=25, max_bytes=MAX_RESPONSE) as response:
        body = response.read()
        supplied = response.headers.get("BW-Signature", "")
    expected = response_signature(peer["secret"], peer["wiki_id"], local_id, headers["BW-Nonce"], body)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid federation response signature.")
    return validate_snapshot(json.loads(body), peer["wiki_id"])
