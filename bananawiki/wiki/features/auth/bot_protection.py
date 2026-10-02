"""Bot protection for public forms: a honeypot field and a signed, single-use form token.

When ``site_settings.bot_protection_enabled`` is on, forms that create
accounts embed ``templates/auth/_bot_fields.html``:

* a honeypot input (``website``) hidden from people and assistive
  technology, which naive bots fill in;
* a token ``<issued-at-ms>.<nonce>.<signature>`` signed with the instance key.

A submission is rejected when the honeypot is filled, the token is missing,
forged, older than :data:`MAX_AGE_SECONDS`, younger than
``BW_MIN_FORM_SECONDS`` (bots submit instantly) or was already used. Unlike
1.4, the check does not switch itself off in test mode and a token cannot be
replayed: its nonce is recorded in ``rate_limit_hits`` on first use.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time

from flask import current_app, request

from ....core import crypto
from ....core.timeutil import now_sql
from ... import settings
from ...db import db

HONEYPOT_FIELD = "website"
TOKEN_FIELD = "_form_time"
MAX_AGE_SECONDS = 2 * 3600
USED_BUCKET = "bot:form_token"
_TOKEN = re.compile(r"([0-9]{1,16})\.([A-Za-z0-9_-]{16})\.([0-9a-f]{32})")


def enabled() -> bool:
    return bool(settings.get("bot_protection_enabled", 1))


def _signature(payload: str) -> str:
    key = crypto.derive(current_app.config["BW"].secret_key, "bot-protection-form-token")
    return hmac.new(key.encode("ascii"), payload.encode("utf-8"), "sha256").hexdigest()[:32]


def new_token() -> str:
    payload = f"{int(time.time() * 1000)}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_signature(payload)}"


def rejection() -> str | None:
    """Why the current form submission looks automated, or None when it passes."""
    if not enabled():
        return None
    if request.form.get(HONEYPOT_FIELD):
        return "honeypot"
    match = _TOKEN.fullmatch(request.form.get(TOKEN_FIELD) or "")
    if match is None:
        return "missing_token"
    issued, nonce, signature = match.groups()
    if not hmac.compare_digest(signature, _signature(f"{issued}.{nonce}")):
        return "invalid_token"
    age = time.time() - int(issued) / 1000
    if age < current_app.config["BW"].min_form_seconds:
        return "too_fast"
    if age > MAX_AGE_SECONDS:
        return "expired_token"
    with db.transaction():
        if db.scalar("SELECT 1 FROM rate_limit_hits WHERE ip = ? AND bucket = ?", (nonce, USED_BUCKET)):
            return "replayed_token"
        db.execute(
            "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)", (nonce, USED_BUCKET, now_sql())
        )
    return None
