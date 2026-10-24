"""OAuth 2.0 provider for platform sign-in on hosted wikis.

Each wiki is a confidential client. Its secret is stored twice: as the 1.4
salted digest ``<salt>$sha256(salt + secret)`` (used to authenticate the wiki)
and encrypted with the platform key (so the runtime can hand it to the wiki
on every start). A wiki whose secret is unknown (rows written by 1.4) gets a
new one on its next start.

Authorisation codes live five minutes, are single use, are stored as SHA-256
digests and may carry a PKCE S256 challenge, which the token request must
then satisfy. Access tokens are opaque, stored as SHA-256 digests, live one
hour and are checked against the database on every use, so revoking them and
suspending or deleting the account take effect at once.

Consent is asked once per wiki and remembered in ``hosting_oauth_grants``
(the owner's own wikis never ask): later sign-ins go straight back to the
wiki. An account sees and withdraws its grants on **Account → Wikis**, which
also signs it out of wikis through :mod:`.signouts`.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import string
from typing import Any
from urllib.parse import urlsplit

from flask import current_app

from ..core import crypto
from ..core.crypto import sha256_hex
from ..core.timeutil import now_sql, sql_in
from . import accounts, settings, signouts, urls
from .db import db
from .runtime import OAuthClient

CODE_SECONDS = 300
TOKEN_SECONDS = 3600
SCOPE = "openid profile"
# Codes issued without a click (remembered consent) per account and minute: a page that loads the
# authorize URL in a loop gets the consent page instead of filling the code table.
AUTO_CODES_PER_MINUTE = 20


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
    return crypto.constant_time_equals(actual, expected)


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
    # urlsplit drops tabs and line breaks, so such a URI would pass the check and break the Location header.
    if not url or len(url) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
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
    from . import domains

    domain = domains.active_domain(inst["id"])
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


def valid_pkce_challenge(challenge: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge))


def pkce_matches(challenge: str, verifier: str) -> bool:
    if not valid_pkce_challenge(challenge) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return crypto.constant_time_equals(digest.decode("ascii").rstrip("="), challenge)


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
        if row is None or not crypto.constant_time_equals(str(row["redirect_uri"] or ""), redirect_uri):
            return None
        if not account_may_sign_in(accounts.get(row["account_id"])):
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
        db.execute("UPDATE hosting_oauth_grants SET last_used_at = ? WHERE account_id = ? AND instance_id = "
                   "(SELECT id FROM instances WHERE oauth_client_id = ?)", (now_sql(), row["account_id"], client_id))
    return token


def token_row(raw: str) -> dict[str, Any] | None:
    if not raw or len(raw) > 256:
        return None
    return db.one("SELECT * FROM hosting_oauth_access_tokens WHERE token_hash = ? AND expires_at > ?",
                  (sha256_hex(raw), now_sql()))


def _shut_out(account: dict[str, Any] | None) -> bool:
    return bool(not account or account["deleted_at"] or account["suspended"]
                or account["approval_status"] != "approved" or account["pending_deletion"])


def account_may_sign_in(account: dict[str, Any] | None) -> bool:
    """Whether *account* may sign in to wikis, or keep using a token it holds for one.

    Not while it must accept new terms of service first (``HOSTING_TERMS_REQUIRED_VERSION``).
    """
    return not _shut_out(account) and not accounts.terms_outdated(account)  # type: ignore[arg-type]


def refusal(account: dict[str, Any]) -> str:
    """The error a wiki is told when :func:`account_may_sign_in` refuses *account*."""
    return "account_suspended" if _shut_out(account) else "terms_not_accepted"


def revoke_account_tokens(account_id: str) -> int:
    return db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ?", (account_id,)).rowcount


def sign_out_everywhere(account_id: str) -> int:
    """End the account's platform sessions on every wiki and drop its access tokens; returns how many wikis."""
    with db.transaction():
        asked = signouts.queue(account_id, "signed_out")
        revoke_account_tokens(account_id)
    return asked


def revoke_account_credentials(account_id: str) -> None:
    """Retire access tokens and unused grants when the account's password changes."""
    db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE account_id = ?", (account_id,))
    revoke_account_tokens(account_id)


def account_authorized_client(account_id: str, client_id: str) -> bool:
    """True while the account holds a live access token for this wiki."""
    if not account_may_sign_in(accounts.get(account_id)):
        return False
    return bool(db.scalar("SELECT 1 FROM hosting_oauth_access_tokens WHERE account_id = ? AND client_id = ? "
                          "AND expires_at > ?", (account_id, client_id, now_sql())))


# Remembered consent and connected wikis


def consent_remembered(account: dict[str, Any], inst: dict[str, Any]) -> bool:
    """Whether *account* already allowed *inst* to know who it is (always for the wiki's owner)."""
    if inst.get("account_id") == account["id"]:
        return True
    return bool(db.scalar("SELECT 1 FROM hosting_oauth_grants WHERE account_id = ? AND instance_id = ?",
                          (account["id"], inst["id"])))


def remember_consent(account_id: str, instance_id: str) -> None:
    db.execute("INSERT OR IGNORE INTO hosting_oauth_grants (account_id, instance_id, granted_at) VALUES (?, ?, ?)",
               (account_id, instance_id, now_sql()))


def _end_wiki_access(account_id: str, inst: dict[str, Any], reason: str) -> int:
    db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE account_id = ? AND client_id = ?",
               (account_id, inst.get("oauth_client_id") or ""))
    asked = signouts.queue(account_id, reason, instance_id=inst["id"])
    db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ? AND client_id = ?",
               (account_id, inst.get("oauth_client_id") or ""))
    return asked


def sign_out_of(account_id: str, instance_id: str) -> bool:
    """Sign *account_id* out of one wiki (its consent stays); False when the account is unknown there."""
    with db.transaction():
        inst = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
        return inst is not None and _end_wiki_access(account_id, inst, "signed_out") > 0


def remove_access(account_id: str, instance_id: str) -> bool:
    """Withdraw consent for one wiki and sign out of it; the next sign-in asks again.

    The link between the portal account and the wiki account stays, so signing
    in again later reaches the same wiki account. False when there was nothing to remove.
    """
    with db.transaction():
        inst = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
        if inst is None:
            return False
        asked = _end_wiki_access(account_id, inst, "access_removed")  # while the grant still names the wiki
        removed = db.execute("DELETE FROM hosting_oauth_grants WHERE account_id = ? AND instance_id = ?",
                             (account_id, instance_id)).rowcount
    return bool(removed or asked)


def connected_wikis(account_id: str) -> list[dict[str, Any]]:
    """The wikis *account_id* signed in to or is linked to, newest sign-in first (terminated ones left out)."""
    rows = db.all(
        "SELECT i.*, g.granted_at, g.last_used_at, l.wiki_username, l.linked_at, "
        "(i.account_id = ?) AS is_owner FROM instances i "
        "LEFT JOIN hosting_oauth_grants g ON g.instance_id = i.id AND g.account_id = ? "
        "LEFT JOIN hosting_oauth_account_links l ON l.instance_id = i.id AND l.account_id = ? "
        "WHERE i.status != 'terminated' AND (g.account_id IS NOT NULL OR l.id IS NOT NULL) "
        "ORDER BY COALESCE(g.last_used_at, l.linked_at, g.granted_at) DESC, i.subdomain",
        (account_id, account_id, account_id),
    )
    return [{**row, "name": urls.original_slug(row["subdomain"]) or row["subdomain"], "url": urls.instance_url(row),
             "signin_url": signin_url(row)} for row in rows]


def signin_url(inst: dict[str, Any], base: str | None = None) -> str:
    """The address that opens the wiki signed in through the portal ("" when the wiki has no address).

    Plain *base* (default: the wiki's platform address) while platform sign-in
    is off, or before the wiki received credentials: a wiki gets them when it
    starts, so one that was running when an administrator turned platform
    sign-in on has no sign-in page yet.
    """
    base = urls.instance_url(inst) if base is None else base
    if not base or not enabled() or not inst.get("oauth_client_id") or inst.get("status") == "terminated":
        return base
    return base.rstrip("/") + "/platform-oauth/login"
