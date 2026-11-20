"""Apps that sign people in with their hosting account (``hosting_oauth_apps``).

Any approved account registers apps under **Account → Developer apps**
(BananaChat, BananaGit, or anything that speaks OAuth 2.0); administrators
register theirs too, and the operator command line can. The protocol is the
one wikis use (:mod:`.oauth`, :mod:`.routes.oauth`), with these differences:

* An app's ``client_id`` is ``bwa_`` and 28 letters or digits; its secret is
  shown once, when the app is created or the secret is rotated, and stored
  as the salted digest wikis' secrets use.
* Codes go only to a redirect URI registered for the app, compared exactly
  (``https``, or ``http`` on the loopback interface for development), and
  apps must use PKCE (S256).
* A new app waits for an administrator (``pending``). Meanwhile only its
  owner and administrators can sign in to it, to try it out. An app an
  administrator registers is approved at once. Renaming an approved app (or
  changing its description or home page, which people see before they allow
  it) sends it back for review; its redirect URIs are its owner's business.
  ``denied`` and ``suspended`` apps sign nobody in; suspending one, or its
  owner, revokes the tokens it holds.
* People allow an app once (:func:`remember_consent`) for the scopes it
  asked for: ``profile`` (the account id and username) and, when the app
  asks, ``email`` (a verified address). An app asking for more is asked
  about again. **Account → Connected wikis and apps** lists them and takes
  access away (:func:`remove_access`).
* Apps never use the wiki-only endpoints (link, link status, unlink,
  sign-outs) and are never told whether an account is an administrator.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from typing import Any
from urllib.parse import urlsplit

from ..core.timeutil import now_sql, sql_in
from . import accounts, attention, events, oauth, settings
from .db import db
from .errors import ServiceError

CLIENT_PREFIX = "bwa_"
STATUSES = ("pending", "approved", "denied", "suspended")
SCOPES = ("profile", "email")
NAME_MAX_LENGTH = 60
DESCRIPTION_MAX_LENGTH = 300
URL_MAX_LENGTH = 512
MAX_REDIRECT_URIS = 5
NOTE_MAX_LENGTH = 500
# Apps an account may register in 24 hours, deleted or not: registering and deleting in a loop stops there.
MAX_CREATED_PER_DAY = 10
ATTENTION_SOURCE = "apps.review"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_CLIENT_ID = re.compile(r"bwa_[a-z0-9]{28}")


def enabled() -> bool:
    """Whether accounts may register apps (platform sign-in must be on too)."""
    return oauth.enabled() and settings.flag("oauth_apps_enabled", True)


def per_account_limit() -> int:
    return max(0, int(settings.get("oauth_apps_per_account", 5) or 0))


# Validation


def _printable(value: str) -> bool:
    return all(char.isprintable() for char in value)


def _url_problem(url: str, *, redirect: bool) -> str | None:
    """Why *url* is not acceptable (a translation key), or None."""
    if not url or len(url) > URL_MAX_LENGTH or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return "hosting.apps.error.url_invalid"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        _port = parts.port  # raises ValueError for a port that is not a number in range
    except ValueError:
        return "hosting.apps.error.url_invalid"
    if not host or parts.username or parts.password or parts.fragment:
        return "hosting.apps.error.url_invalid"
    if parts.scheme == "https" or (parts.scheme == "http" and redirect and host in _LOOPBACK):
        return None
    return "hosting.apps.error.redirect_https" if redirect else "hosting.apps.error.url_invalid"


def parse_redirect_uris(text: str) -> list[str]:
    """The redirect URIs in *text* (one per line), validated; raises ServiceError."""
    uris: list[str] = []
    for line in (text or "").splitlines():
        uri = line.strip()
        if not uri or uri in uris:
            continue
        problem = _url_problem(uri, redirect=True)
        if problem:
            raise ServiceError(problem, url=uri[:80])
        uris.append(uri)
    if not uris:
        raise ServiceError("hosting.apps.error.redirect_required")
    if len(uris) > MAX_REDIRECT_URIS:
        raise ServiceError("hosting.apps.error.redirect_too_many", max=MAX_REDIRECT_URIS)
    return uris


def _clean_fields(name: str, description: str, homepage_url: str) -> tuple[str, str, str]:
    name = " ".join((name or "").split())
    description = (description or "").strip()
    homepage_url = (homepage_url or "").strip()
    if not 2 <= len(name) <= NAME_MAX_LENGTH or not _printable(name):
        raise ServiceError("hosting.apps.error.name", max=NAME_MAX_LENGTH)
    if len(description) > DESCRIPTION_MAX_LENGTH or not all(char.isprintable() or char in "\r\n" for char in description):
        raise ServiceError("hosting.apps.error.description", max=DESCRIPTION_MAX_LENGTH)
    if homepage_url and _url_problem(homepage_url, redirect=False):
        raise ServiceError("hosting.apps.error.homepage")
    return name, description, homepage_url


def _new_client_id() -> str:
    return CLIENT_PREFIX + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(28))


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        uris = json.loads(row.get("redirect_uris") or "[]")
    except ValueError:
        uris = []
    return {**row, "redirect_uris": [uri for uri in uris if isinstance(uri, str)] if isinstance(uris, list) else []}


# Lookups


def get(app_id: str) -> dict[str, Any] | None:
    return _row(db.one("SELECT * FROM hosting_oauth_apps WHERE id = ?", ((app_id or "")[:64],)))


def owned(account_id: str, app_id: str) -> dict[str, Any] | None:
    return _row(db.one("SELECT * FROM hosting_oauth_apps WHERE id = ? AND account_id = ?",
                       ((app_id or "")[:64], account_id)))


def for_client(client_id: str) -> dict[str, Any] | None:
    if not client_id or not _CLIENT_ID.fullmatch(client_id):
        return None
    return _row(db.one("SELECT * FROM hosting_oauth_apps WHERE client_id = ?", (client_id,)))


def for_owner(account_id: str) -> list[dict[str, Any]]:
    return [_row(row) for row in db.all(  # type: ignore[misc]
        "SELECT a.*, (SELECT COUNT(*) FROM hosting_oauth_app_grants g WHERE g.app_id = a.id) AS users "
        "FROM hosting_oauth_apps a WHERE a.account_id = ? ORDER BY a.created_at DESC", (account_id,))]


def list_all(status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    where, values = ("WHERE a.status = ? ", [status]) if status in STATUSES else ("", [])
    rows = db.all(
        "SELECT a.*, o.username AS owner_name, "
        "(SELECT COUNT(*) FROM hosting_oauth_app_grants g WHERE g.app_id = a.id) AS users "
        "FROM hosting_oauth_apps a LEFT JOIN accounts o ON o.id = a.account_id " + where +
        "ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.created_at DESC LIMIT ?",
        (*values, max(1, min(int(limit), 1000))),
    )
    return [_row(row) for row in rows]  # type: ignore[misc]


def authenticate(client_id: str, secret: str) -> dict[str, Any] | None:
    """The app whose credentials these are, whatever its status (the token endpoint checks that), or None."""
    app = for_client(client_id)
    if app is None or not oauth.check_secret(app.get("client_secret_hash"), secret):
        return None
    return app


def redirect_allowed(app: dict[str, Any], redirect_uri: str) -> bool:
    return bool(redirect_uri) and any(secrets.compare_digest(redirect_uri.encode(), uri.encode())
                                      for uri in app["redirect_uris"])


def requested_scopes(value: str | None) -> str:
    """The scopes granted for a request's ``scope`` (``profile`` always, ``email`` when asked), in order."""
    asked = set((value or "").split())
    return " ".join(scope for scope in SCOPES if scope == "profile" or scope in asked)


def verified_email(account: dict[str, Any]) -> str:
    """The address an app with the ``email`` scope is told: only a verified one an administrator did not flag."""
    if not account.get("email") or not account.get("email_verified_at") or account.get("email_flagged_invalid"):
        return ""
    return str(account["email"])


def owner_active(app: dict[str, Any]) -> bool:
    return oauth.account_may_sign_in(accounts.get(app["account_id"]))


def usable_by(app: dict[str, Any], account: dict[str, Any]) -> bool:
    """Whether *account* may sign in to *app*: approved apps (of an owner in good standing) for everyone,
    apps waiting for review for their owner and administrators only."""
    if app["status"] == "approved":
        return owner_active(app)
    if app["status"] == "pending":
        return account["id"] == app["account_id"] or bool(account.get("is_admin"))
    return False


def token_usable(app: dict[str, Any], account: dict[str, Any] | None) -> bool:
    """Whether a token *app* holds for *account* still answers (the app was not suspended meanwhile)."""
    return account is not None and usable_by(app, account)


# Registering and changing apps


def create(owner: dict[str, Any], name: str, description: str, homepage_url: str, redirect_uris: str, *,
           actor_id: str | None, approved: bool | None = None) -> tuple[dict[str, Any], str]:
    """Register an app for *owner*; returns ``(app, client_secret)``.

    *approved* defaults to whether the owner is an administrator.
    """
    name, description, homepage_url = _clean_fields(name, description, homepage_url)
    uris = parse_redirect_uris(redirect_uris)
    approved = bool(owner.get("is_admin")) if approved is None else approved
    secret = secrets.token_urlsafe(48)
    with db.transaction():
        if not owner.get("is_admin") and actor_id == owner["id"]:
            live = int(db.scalar("SELECT COUNT(*) FROM hosting_oauth_apps WHERE account_id = ?", (owner["id"],)) or 0)
            if live >= per_account_limit():
                raise ServiceError("hosting.apps.error.limit", max=per_account_limit())
            created = int(db.scalar(
                "SELECT COUNT(*) FROM hosting_events WHERE subject_type = 'account' AND subject_id = ? "
                "AND action = 'oauth.app.created' AND created_at > ?", (owner["id"], sql_in(hours=-24))) or 0)
            if created >= MAX_CREATED_PER_DAY:
                raise ServiceError("hosting.apps.error.too_many", limit=MAX_CREATED_PER_DAY)
        app_id, now = secrets.token_hex(8), now_sql()
        db.insert("hosting_oauth_apps", {
            "id": app_id, "account_id": owner["id"], "name": name, "description": description,
            "homepage_url": homepage_url, "redirect_uris": json.dumps(uris), "client_id": _new_client_id(),
            "client_secret_hash": oauth.hash_secret(secret), "status": "approved" if approved else "pending",
            "reviewed_by": actor_id if approved else None, "reviewed_at": now if approved else None,
            "created_at": now, "updated_at": now,
        })
        # The log, not the table, is counted above: apps deleted since still count.
        events.record("account", owner["id"], "oauth.app.created", actor_id, f"{name} ({app_id})")
        if not approved:
            attention.created(ATTENTION_SOURCE, app_id)
    return get(app_id), secret  # type: ignore[return-value]


def update(app: dict[str, Any], name: str, description: str, homepage_url: str, redirect_uris: str, *,
           actor: dict[str, Any]) -> dict[str, Any]:
    """Change an app; an approved app whose public details changed goes back to review (not an administrator's)."""
    name, description, homepage_url = _clean_fields(name, description, homepage_url)
    uris = parse_redirect_uris(redirect_uris)
    public_changed = (name, description, homepage_url) != (app["name"], app["description"], app["homepage_url"])
    values: dict[str, Any] = {"name": name, "description": description, "homepage_url": homepage_url,
                              "redirect_uris": json.dumps(uris), "updated_at": now_sql()}
    back_to_review = public_changed and app["status"] == "approved" and not actor.get("is_admin")
    if back_to_review:
        values.update(status="pending", reviewed_by=None, reviewed_at=None, review_note="")
    with db.transaction():
        db.update("hosting_oauth_apps", values, "id = ?", (app["id"],))
        events.record("account", app["account_id"], "oauth.app.updated", actor["id"],
                      f"{name} ({app['id']})" + (": back to review" if back_to_review else ""))
        if back_to_review:
            attention.created(ATTENTION_SOURCE, app["id"])
    return get(app["id"])  # type: ignore[return-value]


def _revoke_app_tokens(app: dict[str, Any]) -> None:
    db.execute("DELETE FROM hosting_oauth_access_tokens WHERE client_id = ?", (app["client_id"],))
    db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE client_id = ?", (app["client_id"],))


def rotate_secret(app: dict[str, Any], *, actor_id: str | None) -> str:
    """A new client secret (the old one stops working, with the tokens issued so far); returns it."""
    secret = secrets.token_urlsafe(48)
    with db.transaction():
        db.update("hosting_oauth_apps", {"client_secret_hash": oauth.hash_secret(secret), "updated_at": now_sql()},
                  "id = ?", (app["id"],))
        _revoke_app_tokens(app)
        events.record("account", app["account_id"], "oauth.app.secret_rotated", actor_id, f"{app['name']} ({app['id']})")
    return secret


def delete(app: dict[str, Any], *, actor_id: str | None) -> None:
    with db.transaction():
        _revoke_app_tokens(app)
        db.execute("DELETE FROM hosting_oauth_apps WHERE id = ?", (app["id"],))
        events.record("account", app["account_id"], "oauth.app.deleted", actor_id, f"{app['name']} ({app['id']})")


DECISIONS = {"approve": "approved", "deny": "denied", "suspend": "suspended"}


def review(app: dict[str, Any], decision: str, *, actor_id: str | None, note: str = "") -> dict[str, Any]:
    """Approve, deny or suspend an app; denying or suspending revokes its tokens. The owner gets a notice."""
    status = DECISIONS.get(decision)
    if status is None:
        raise ServiceError("hosting.apps.error.decision")
    note = (note or "").strip()[:NOTE_MAX_LENGTH]
    with db.transaction():
        db.update("hosting_oauth_apps", {"status": status, "review_note": note, "reviewed_by": actor_id,
                                         "reviewed_at": now_sql(), "updated_at": now_sql()}, "id = ?", (app["id"],))
        if status != "approved":
            _revoke_app_tokens(app)
        events.record("account", app["account_id"], f"oauth.app.{status}", actor_id,
                      f"{app['name']} ({app['id']})" + (f": {note}" if note else ""))
        if app["status"] != status and status in ("approved", "denied"):
            attention.decided(app["account_id"], ATTENTION_SOURCE, status, object_id=app["id"], detail=app["name"])
    return get(app["id"])  # type: ignore[return-value]


def touch(app_id: str) -> None:
    db.execute("UPDATE hosting_oauth_apps SET last_used_at = ? WHERE id = ?", (now_sql(), app_id))


# Consent


def granted_scope(account_id: str, app_id: str) -> str | None:
    """The scopes *account_id* allowed *app_id* (``None`` when it never did)."""
    return db.scalar("SELECT scope FROM hosting_oauth_app_grants WHERE account_id = ? AND app_id = ?",
                     (account_id, app_id))


def consent_covers(account_id: str, app_id: str, scope: str) -> bool:
    granted = granted_scope(account_id, app_id)
    return granted is not None and set(scope.split()) <= set(granted.split())


def remember_consent(account_id: str, app_id: str, scope: str) -> None:
    """Store the consent, keeping what was allowed before (asking for ``profile`` again keeps ``email``)."""
    with db.transaction():
        before = set((granted_scope(account_id, app_id) or "").split())
        merged = " ".join(s for s in SCOPES if s in before | set(scope.split()))
        db.execute("INSERT INTO hosting_oauth_app_grants (account_id, app_id, scope, granted_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(account_id, app_id) DO UPDATE SET scope = excluded.scope",
                   (account_id, app_id, merged, now_sql()))


def connected(account_id: str) -> list[dict[str, Any]]:
    """The apps *account_id* allowed, last used first."""
    rows = db.all(
        "SELECT a.id, a.name, a.description, a.homepage_url, a.status, a.client_id, o.username AS owner_name, "
        "g.scope, g.granted_at, g.last_used_at FROM hosting_oauth_app_grants g "
        "JOIN hosting_oauth_apps a ON a.id = g.app_id LEFT JOIN accounts o ON o.id = a.account_id "
        "WHERE g.account_id = ? ORDER BY COALESCE(g.last_used_at, g.granted_at) DESC, a.name", (account_id,),
    )
    return [{**row, "scopes": row["scope"].split()} for row in rows]


def remove_access(account_id: str, app_id: str) -> bool:
    """Withdraw consent for one app and revoke what it holds for the account; False when nothing was allowed."""
    with db.transaction():
        app = get(app_id)
        if app is None:
            return False
        removed = db.execute("DELETE FROM hosting_oauth_app_grants WHERE account_id = ? AND app_id = ?",
                             (account_id, app["id"])).rowcount
        db.execute("DELETE FROM hosting_oauth_access_tokens WHERE account_id = ? AND client_id = ?",
                   (account_id, app["client_id"]))
        db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE account_id = ? AND client_id = ?",
                   (account_id, app["client_id"]))
    return bool(removed)


# Accounts going away or merging (run in the caller's transaction)


def forget_account(account_id: str) -> None:
    """A deleted account: its consent goes, and the apps it registered with their tokens."""
    db.execute("DELETE FROM hosting_oauth_app_grants WHERE account_id = ?", (account_id,))
    for app in db.all("SELECT id, name, client_id, account_id FROM hosting_oauth_apps WHERE account_id = ?",
                      (account_id,)):
        _revoke_app_tokens(app)
        db.execute("DELETE FROM hosting_oauth_apps WHERE id = ?", (app["id"],))


def merge_accounts(source_id: str, target_id: str) -> None:
    """The target of a merge takes over the source's apps and the consent it gave (the widest of the two)."""
    db.execute("UPDATE hosting_oauth_apps SET account_id = ? WHERE account_id = ?", (target_id, source_id))
    for grant in db.all("SELECT * FROM hosting_oauth_app_grants WHERE account_id = ?", (source_id,)):
        before = set((granted_scope(target_id, grant["app_id"]) or "").split())
        merged = " ".join(s for s in SCOPES if s in before | set(grant["scope"].split()))
        db.execute("INSERT INTO hosting_oauth_app_grants (account_id, app_id, scope, granted_at, last_used_at) "
                   "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, app_id) DO UPDATE SET scope = excluded.scope",
                   (target_id, grant["app_id"], merged, grant["granted_at"], grant["last_used_at"]))
    db.execute("DELETE FROM hosting_oauth_app_grants WHERE account_id = ?", (source_id,))
