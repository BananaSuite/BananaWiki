"""BananaWiki Userbot SDK."""

from __future__ import annotations

import json
from urllib import request as _request
from urllib.parse import urlparse


class _NoRedirect(_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UserbotClient:
    """Small standard-library client for the BananaWiki Userbot API."""

    def __init__(self, base_url: str, api_key: str):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an absolute http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _call(self, method: str, path: str, payload=None):
        body = None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = _request.Request(
            url=f"{self.base_url}{path}",
            method=method,
            headers=headers,
            data=body,
        )
        with _request.build_opener(_request.ProxyHandler({}), _NoRedirect()).open(req, timeout=15) as resp:  # noqa: S310
            body = resp.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("Userbot response exceeds 8 MiB")
            return json.loads(body.decode("utf-8"))

    def me(self):
        """Return account/profile metadata."""
        return self._call("GET", "/api/v1/userbot/me")

    def update_profile(self, *, real_name="", bio="", page_published=None):
        """Update profile fields through the Userbot API."""
        payload = {"real_name": real_name, "bio": bio}
        if page_published is not None:
            payload["page_published"] = bool(page_published)
        return self._call("POST", "/api/v1/userbot/profile", payload=payload)
