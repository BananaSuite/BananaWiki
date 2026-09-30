"""OAuth 2.0 provider for platform sign-in on hosted wikis.

Each wiki is a confidential client. Its secret is stored twice: as the 1.4
salted digest ``<salt>$sha256(salt + secret)`` (used to authenticate the wiki)
and encrypted with the platform key (so the runtime can hand it to the wiki
on every start; 1.4 only passed it on the first start, so a restarted wiki
lost platform sign-in). A wiki whose secret is unknown (1.4 rows) gets a new
one on its next start.

Authorisation codes live five minutes, are single use, are stored as SHA-256
digests and may carry a PKCE S256 challenge, which the token request must
then satisfy. Access tokens are opaque, stored as SHA-256 digests, live one
hour and are checked against the database on every use, so revoking them and
suspending or deleting the account take effect at once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from typing import Any
from urllib.parse import urlsplit

from flask import current_app

from ..core import crypto
from ..core.crypto import sha256_hex
from ..core.timeutil import now_sql, sql_in
from . import settings, urls
from .db import db
from .runtime import OAuthClient

CODE_SECONDS = 300
TOKEN_SECONDS = 3600
SCOPE = "openid profile"


def enabled() -> bool:
    return settings.flag("platform_oauth_enabled")


def _key() -> str:
    return current_app.config["HOSTING"].secret_key


def hash_secret(secret: str) -> str:
    salt = secrets.token_hex(16)
    return f"{salt}${hashlib.sha256((salt + secret).encode('utf-8')).hexdigest()}"


def check_secret(stored: str | None, candidate: str) -> bool:
    if not stored or "$" not in stored or not candidate:
        return False
    salt, expected = stored.split("$", 1)
    actual = hashlib.sha256((salt + candidate).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def _new_client_id() -> str:
    return "bw_" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(28))


def rotate_credentials(instance_id: str) -> tuple[str, str]:
    """Issue a new client id and secret for a wiki; returns them."""
    client_id, secret = _new_client_id(), secrets.token_urlsafe(48)
    with db.transaction():
        db.execute("DELETE FROM hosting_oauth_access_tokens WHERE client_id IN (SELECT oauth_client_id FROM instances "
                   "WHERE id = ?)", (instance_id,))
        db.update("instances", {"oauth_client_id": client_id, "oauth_client_secret_hash": hash_secret(secret),
                                "oauth_client_secret_encrypted": crypto.encrypt(_key(), secret)},
                  "id = ?", (instance_id,))
    return client_id, secret


def client_for(inst: dict[str, Any]) -> OAuthClient | None:
    """The credentials to give a wiki, creating or re-issuing them when needed."""
    if not enabled() or inst["status"] == "terminated":
        return None
    secret = crypto.decrypt(_key(), inst.get("oauth_client_secret_encrypted"))
    client_id = inst.get("oauth_client_id")
    if not client_id or not secret or not check_secret(inst.get("oauth_client_secret_hash"), secret):
        client_id, secret = rotate_credentials(inst["id"])
    return OAuthClient(client_id=client_id, client_secret=secret, portal_base=urls.portal_base_url(),
                       instance_id=inst["id"])


def instance_for_client(client_id: str) -> dict[str, Any] | None:
    if not client_id:
        return None
    return db.one("SELECT * FROM instances WHERE oauth_client_id = ? AND status != 'terminated'", (client_id,))


def authenticate_client(client_id: str, secret: str) -> dict[str, Any] | None:
    inst = instance_for_client(client_id)
    if inst is None or not check_secret(inst.get("oauth_client_secret_hash"), secret):
        return None
    return inst


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def redirect_allowed(inst: dict[str, Any], redirect_uri: str) -> bool:
    """A code may only be delivered to the wiki's own platform URL or verified domain."""
    origin = _origin(redirect_uri)
    if not origin:
        return False
    allowed = set()
    platform = _origin(urls.instance_url(inst))
    if platform:
        allowed.add(platform)
    domain = db.scalar("SELECT domain FROM instance_custom_domains WHERE instance_id = ? AND verified_at IS NOT NULL "
                       "AND verified_until > ?", (inst["id"], now_sql()))
    if domain:
        allowed.add(f"https://{domain.lower()}")
    return origin in allowed


def issue_code(client_id: str, account_id: str, redirect_uri: str, challenge: str, method: str) -> str:
    code = secrets.token_urlsafe(32)
    db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE expires_at < ?", (now_sql(),))
    db.insert("hosting_oauth_authorization_codes", {
        "code": sha256_hex(code), "client_id": client_id, "account_id": account_id, "redirect_uri": redirect_uri,
        "expires_at": sql_in(seconds=CODE_SECONDS), "created_at": now_sql(), "code_challenge": challenge,
        "code_challenge_method": method,
    })
    return code


def pkce_matches(challenge: str, verifier: str) -> bool:
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest())
    return hmac.compare_digest(digest.decode("ascii").rstrip("="), challenge)


def redeem_code(code: str, client_id: str, redirect_uri: str, verifier: str) -> str | None:
    """Exchange a code for an access token; returns the raw token or None.

    A code presented a second time was intercepted (or replayed): the tokens
    this wiki holds for that account are revoked (RFC 6749 section 4.1.2).
    """
    if not code:
        return None
    with db.transaction():
        row = db.one("SELECT * FROM hosting_oauth_authorization_codes WHERE code = ? AND client_id = ? "
                     "AND expires_at > ?", (sha256_hex(code), client_id, now_sql()))
        if row is not None and row["used"]:
            db.execute("DELETE FROM hosting_oauth_access_tokens WHERE client_id = ? AND account_id = ?",
                       (client_id, row["account_id"]))
            return None
        if row is None or not hmac.compare_digest(str(row["redirect_uri"] or ""), redirect_uri):
            return None
        if row["code_challenge"] and not pkce_matches(row["code_challenge"], verifier):
            return None
        if db.execute("UPDATE hosting_oauth_authorization_codes SET used = 1 WHERE id = ? AND used = 0",
                      (row["id"],)).rowcount != 1:
            return None
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM hosting_oauth_access_tokens WHERE expires_at < ?", (now_sql(),))
        db.insert("hosting_oauth_access_tokens", {
            "token_hash": sha256_hex(token), "client_id": client_id, "account_id": row["account_id"],
            "scope": SCOPE, "expires_at": sql_in(seconds=TOKEN_SECONDS), "created_at": now_sql(),
        })
    return token


def token_row(raw: str) -> dict[str, Any] | None:
    if not raw or len(raw) > 256:
        return None
    return db.one("SELECT * FROM hosting_oauth_access_tokens WHERE token_hash = ? AND expires_at > ?",
                  (sha256_hex(raw), now_sql()))


def account_may_sign_in(account: dict[str, Any] | None) -> bool:
    return bool(account and not account["deleted_at"] and not account["suspended"]
                and account["approval_status"] == "approved" and not account["pending_deletion"])


def revoke_account_tokens(account_id: str) -> int:
    return db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ?", (account_id,)).rowcount


def account_authorized_client(account_id: str, client_id: str) -> bool:
    """True while the account holds a live access token for this wiki."""
    return bool(db.scalar("SELECT 1 FROM hosting_oauth_access_tokens WHERE account_id = ? AND client_id = ? "
                          "AND expires_at > ?", (account_id, client_id, now_sql())))
