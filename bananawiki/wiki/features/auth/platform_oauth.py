"""Single sign-on with the hosting portal (OAuth 2.0 authorization code + PKCE).

Configured by ``BW_PLATFORM_OAUTH_*`` (see ``config.platform_oauth``). The rules:

* Wiki accounts are found through ``platform_oauth_links`` (the portal's
  stable account id), not by matching usernames. Links made by 1.4, which
  only the portal knows, are adopted on first use through its link-status
  endpoint.
* New accounts are created under the wiki's sign-up policy (closed sign-up
  needs an invite code, ``approval_required`` makes them pending, usernames
  follow the wiki's rules) by :func:`signup.register`.
* Access tokens are used once to read the profile and then dropped: they are
  never stored, neither in the database nor in the cookie session.
* Server-to-server calls authenticate with HTTP Basic
  ``client_id:client_secret`` (the portal requires it on ``link-status``;
  the secret is never sent in a form or JSON body) and go through
  :mod:`bananawiki.core.http` (timeouts, no redirects, bounded bodies) to
  the portal's origin only; private addresses (and plain HTTP) are allowed
  only when the configured portal itself is on a private network.
* Errors shown to people are translated messages; details go to the log.
* The first platform sign-in on this wiki asks what to do (``/platform-oauth/connect``):
  create a new wiki account (a free name is suggested when the portal name is
  taken, reserved or invalid here) or connect an account the person already
  has here, proven with its password and two-step code. Nothing is matched by
  user name alone.
* Single sign-out: the ``auth.platform_signouts`` job fetches the portal's
  sign-outs (``/oauth/signouts``) and ends the platform sessions (those with
  ``auth_method`` ``platform_oauth…``) of the linked wiki account that
  started before each sign-out. Sessions started with the wiki's own password
  are the wiki's business and stay.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

from flask import current_app, session, url_for

from ....core import crypto, http
from ....core.timeutil import now_sql, parse, to_sql
from ... import accounts
from ...db import db

log = logging.getLogger("bananawiki.platform_oauth")

STATE_KEY = "_platform_oauth_states"
PENDING_KEY = "_platform_oauth_pending"
STATE_TTL_SECONDS = 600
MAX_STATES = 5
MAX_RESPONSE_BYTES = 256 * 1024
REQUIRED = ("client_id", "client_secret", "authorize_url", "token_url", "userinfo_url", "portal_base")


class OAuthError(Exception):
    """A failed sign-on step; ``key`` is the translation key shown to the person."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key


def config() -> dict[str, str] | None:
    """The OAuth settings, or None when single sign-on is off or incomplete."""
    values = current_app.config["BW"].platform_oauth
    if not values or not all(values.get(key) for key in REQUIRED):
        return None
    return values


def instance_id() -> str:
    return current_app.config["BW"].platform_instance_id


# State and PKCE


def _verifier(state: str) -> str:
    """PKCE code verifier, derived from the state so nothing secret is stored in the cookie."""
    return crypto.derive(current_app.config["BW"].secret_key, "platform-oauth-pkce:" + state)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def redirect_uri(purpose: str) -> str:
    endpoint = "auth.platform_oauth_link_callback" if purpose == "link" else "auth.platform_oauth_callback"
    return url_for(endpoint, _external=True)


def authorize_url(purpose: str, *, next_url: str = "", user_id: str | None = None) -> str:
    """Remember a new state in the session and build the portal's authorize URL."""
    cfg = config()
    assert cfg is not None
    now = time.time()
    states = {key: value for key, value in (session.get(STATE_KEY) or {}).items()
              if now - value.get("at", 0) < STATE_TTL_SECONDS}
    while len(states) >= MAX_STATES:
        states.pop(min(states, key=lambda key: states[key]["at"]))
    state = secrets.token_urlsafe(24)
    states[state] = {"purpose": purpose, "next": next_url, "uid": user_id, "at": now}
    session[STATE_KEY] = states
    query = urlencode({
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri(purpose),
        "state": state,
        "code_challenge": _challenge(_verifier(state)),
        "code_challenge_method": "S256",
    })
    separator = "&" if "?" in cfg["authorize_url"] else "?"
    return f"{cfg['authorize_url']}{separator}{query}"


def consume_state(state: str, purpose: str) -> dict[str, Any]:
    states = dict(session.get(STATE_KEY) or {})
    entry = states.pop(state, None) if state else None
    session[STATE_KEY] = states
    if not entry or entry.get("purpose") != purpose or time.time() - entry.get("at", 0) > STATE_TTL_SECONDS:
        raise OAuthError("auth.oauth.error.expired")
    return entry


# Talking to the portal


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port


def _schemes(portal_base: str) -> tuple[str, ...]:
    # A portal on plain HTTP is only possible on a private network (checked below).
    return ("https", "http") if urlsplit(portal_base).scheme == "http" else ("https",)


def portal_is_private(portal_base: str) -> bool:
    """True when the configured portal host resolves only to local-network addresses."""
    try:
        _scheme, host, port = http.check_url(portal_base, schemes=("https", "http"))
        infos = http.resolve(host, port, allow_private=True)
    except http.HttpError:
        return False
    return not any(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]).is_global for info in infos)


def _client_auth(cfg: dict[str, str]) -> dict[str, str]:
    """HTTP Basic ``client_id:client_secret``, which the portal requires on its server-to-server endpoints."""
    raw = f"{cfg['client_id']}:{cfg['client_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _call(method: str, url: str, *, form: dict[str, Any] | None = None, json_body: Any = None,
          headers: dict[str, str] | None = None, client_auth: bool = False) -> tuple[int, Any]:
    """One server-to-server call to the portal; returns (status, parsed JSON or None)."""
    cfg = config()
    assert cfg is not None
    if not url or _origin(url) != _origin(cfg["portal_base"]):
        raise OAuthError("auth.oauth.error.portal", f"{url!r} is not on the portal's origin")
    sent = {"Accept": "application/json", **(headers or {}), **(_client_auth(cfg) if client_auth else {})}
    body = None
    if form is not None:
        body = urlencode(form).encode("utf-8")
        sent["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    private = portal_is_private(cfg["portal_base"])
    schemes = _schemes(cfg["portal_base"]) if private else ("https",)
    try:
        response = http.request(method, url, headers=sent, body=body, max_bytes=MAX_RESPONSE_BYTES,
                                allow_private=private, schemes=schemes)
    except http.HttpError as error:
        raise OAuthError("auth.oauth.error.portal", str(error)) from error
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    return response.status, data


def exchange_code(code: str, state: str, purpose: str) -> str:
    """Trade an authorization code for an access token (kept in memory only)."""
    cfg = config()
    assert cfg is not None
    status, data = _call("POST", cfg["token_url"], form={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri(purpose),
        "code_verifier": _verifier(state),
    }, client_auth=True)
    token = data.get("access_token") if status == 200 and isinstance(data, dict) else None
    if not token or not isinstance(token, str):
        raise OAuthError("auth.oauth.error.portal", f"token endpoint answered {status}")
    return token


def fetch_profile(token: str) -> tuple[str, str]:
    """(portal account id, portal username) for an access token."""
    cfg = config()
    assert cfg is not None
    status, data = _call("GET", cfg["userinfo_url"], headers={"Authorization": f"Bearer {token}"})
    if status != 200 or not isinstance(data, dict):
        raise OAuthError("auth.oauth.error.portal", f"userinfo answered {status}")
    account_id = str(data.get("sub") or "").strip()
    username = str(data.get("username") or data.get("preferred_username") or "").strip()
    if not account_id or not username:
        raise OAuthError("auth.oauth.error.portal", "userinfo without sub/username")
    return account_id[:200], username[:200]


def _portal_link_status(user_id: str = "", *, account_id: str = "") -> dict[str, Any]:
    """The portal's link of a wiki account (or, by *account_id*, of a portal account) on this wiki."""
    cfg = config()
    assert cfg is not None
    if not cfg.get("link_status_url"):
        return {}
    key = {"wiki_user_id": user_id} if user_id else {"account_id": account_id}
    url = f"{cfg['link_status_url']}?{urlencode({'instance_id': instance_id(), **key})}"
    _status, data = _call("GET", url, client_auth=True)
    return data if isinstance(data, dict) and data.get("linked") else {}


def _portal_post(key: str, payload: dict[str, Any]) -> None:
    cfg = config()
    assert cfg is not None
    if not cfg.get(key):
        return
    status, data = _call("POST", cfg[key], json_body={**payload, "instance_id": instance_id()}, client_auth=True)
    if status != 200 or not (isinstance(data, dict) and data.get("ok")):
        raise OAuthError("auth.oauth.error.account_taken" if status == 409 else "auth.oauth.error.portal",
                         f"{key} answered {status}")


# Links


def link_of(user_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM platform_oauth_links WHERE user_id = ?", (user_id,))


def linked_user(account_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT u.* FROM platform_oauth_links l JOIN users u ON u.id = l.user_id WHERE l.account_id = ?",
        (account_id,),
    )


def _store_link(user: dict[str, Any], account_id: str, portal_username: str) -> None:
    db.execute(
        "INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) VALUES (?, ?, ?, ?)",
        (account_id, user["id"], portal_username, now_sql()),
    )


def adopt_portal_link(user: dict[str, Any], account_id: str, portal_username: str) -> bool | None:
    """For an account without a local link: True if the portal already links it to *account_id*
    (a 1.4 link, which is then stored locally), False if the portal links it to someone else, None if unlinked."""
    status = _portal_link_status(user["id"])
    if not status:
        return None
    if str(status.get("hosting_account_id") or "") != account_id:
        return False
    _store_link(user, account_id, portal_username)
    return True


def realign_portal_link(account_id: str, portal_username: str) -> dict[str, Any] | None:
    """The wiki account the portal links *account_id* to, its local link made to match; None without one.

    Merging two portal accounts moves the source's links to the target on the
    portal only: the row here still names the source, so the target would be
    refused or handed a second account. The portal is the authority on which
    portal account a wiki account belongs to, unless it still links the
    account named here too (a stale link): then nothing changes. A portal
    without this lookup answers no link, and nothing changes either.
    """
    status = _portal_link_status(account_id=account_id)
    user_id = str(status.get("wiki_user_id") or "")
    if not user_id or str(status.get("hosting_account_id") or "") != account_id:
        return None
    user = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if user is None:
        return None
    current = link_of(user_id)
    if current is not None and _portal_link_status(account_id=current["account_id"]):
        return None
    with db.transaction():
        db.execute("DELETE FROM platform_oauth_links WHERE user_id = ? OR account_id = ?", (user_id, account_id))
        _store_link(user, account_id, portal_username)
    log.info("Platform link of wiki account %s realigned to portal account %s", user_id, account_id)
    return user


def link(user: dict[str, Any], account_id: str, portal_username: str) -> None:
    """Link *user* to a portal account, on the portal and locally."""
    if linked_user(account_id) is not None or link_of(user["id"]) is not None:
        raise OAuthError("auth.oauth.error.account_taken")
    _portal_post("link_url", {"account_id": account_id, "wiki_user_id": user["id"],
                              "wiki_username": user["username"]})
    _store_link(user, account_id, portal_username)


def unlink(user: dict[str, Any]) -> None:
    existing = link_of(user["id"])
    if existing is None:
        return
    # The wiki account too: after a merge on the portal its link there names another account.
    _portal_post("unlink_url", {"account_id": existing["account_id"], "wiki_user_id": user["id"]})
    db.execute("DELETE FROM platform_oauth_links WHERE user_id = ?", (user["id"],))


# Choosing a wiki user name


def name_problem(username: str) -> str | None:
    """Why *username* cannot be used for a new account here (a translation key), or None."""
    try:
        accounts.validate_username(username)
    except accounts.AccountError as error:
        return error.key
    return "auth.error.username_taken" if accounts.username_taken(username) else None


def suggest_username(portal_username: str) -> str:
    """A name a new wiki account can take: the portal's when it is free, else the closest free one."""
    base = "".join(char if char.isascii() and (char.isalnum() or char in "_-") else "_"
                   for char in (portal_username or "").strip())[:44].strip("_-") or "member"
    if len(base) < 3:
        base = (base + "_member")[:44]
    if name_problem(base) is None:
        return base
    for number in range(2, 100):
        candidate = f"{base}{number}"
        if name_problem(candidate) is None:
            return candidate
    while True:
        candidate = f"{base[:40]}_{secrets.token_hex(3)}"
        if name_problem(candidate) is None:
            return candidate


# Single sign-out

SIGNOUT_PAGES_PER_RUN = 10
SIGNOUT_REASON = "platform_signout"


def signouts_url(cfg: dict[str, str]) -> str:
    return cfg.get("signouts_url") or cfg["portal_base"].rstrip("/") + "/oauth/signouts"


def _signout_cursor() -> int:
    value = db.scalar("SELECT signout_cursor FROM platform_oauth_state WHERE id = 1")
    return int(value or 0)


def _save_signout_cursor(cursor: int) -> None:
    db.execute(
        "INSERT INTO platform_oauth_state (id, signout_cursor, signouts_checked_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET signout_cursor = excluded.signout_cursor, "
        "signouts_checked_at = excluded.signouts_checked_at",
        (cursor, now_sql()),
    )


def end_platform_sessions(user_id: str, started_before: str) -> int:
    """End *user_id*'s sessions that began through platform sign-in at or before *started_before*."""
    return db.execute(
        "UPDATE user_sessions SET revoked_at = ?, revoked_reason = ? WHERE user_id = ? AND revoked_at IS NULL "
        "AND auth_method LIKE 'platform_oauth%' AND created_at <= ?",
        (now_sql(), SIGNOUT_REASON, user_id, started_before),
    ).rowcount


def _signed_out_user(account_id: str, wiki_user_id: str) -> str | None:
    """The wiki account a sign-out of portal account *account_id* is about, or None.

    The link stored here decides. The portal's ``wiki_user_id`` is used only
    for an account without a link here (a link made by 1.4, which only the
    portal knows): never to sign out an account linked to someone else.
    """
    user = linked_user(account_id)
    if user is not None:
        return user["id"]
    if wiki_user_id and link_of(wiki_user_id) is None and db.scalar("SELECT 1 FROM users WHERE id = ?",
                                                                      (wiki_user_id,)):
        return wiki_user_id
    return None


def _valid_event(event: Any, cursor: int) -> tuple[int, str, str, str] | None:
    if not isinstance(event, dict):
        return None
    event_id, account_id = event.get("id"), event.get("account_id")
    raw_at = event.get("at")
    wiki_user_id, at = event.get("wiki_user_id") or "", parse(raw_at, bounded=True) if isinstance(raw_at, str) else None
    if (type(event_id) is not int or not cursor < event_id < 2**63 or not isinstance(account_id, str)
            or not account_id or len(account_id) > 200 or not isinstance(wiki_user_id, str)
            or len(wiki_user_id) > 128 or at is None):
        return None
    # Never later than now: a sign-out cannot reach sessions that start after it is applied.
    return event_id, account_id, wiki_user_id, min(to_sql(at), now_sql())


def apply_signouts() -> int:
    """Job: carry out the portal's sign-outs for this wiki; returns how many sessions ended.

    Does nothing without platform sign-in. A portal without this endpoint
    (404) is left alone; other failures are logged without what the portal
    sent and retried on the next run, from the same cursor.
    """
    cfg = config()
    if cfg is None:
        return 0
    url, cursor, ended = signouts_url(cfg), _signout_cursor(), 0
    for _page in range(SIGNOUT_PAGES_PER_RUN):
        query = {"after": cursor, **({"instance_id": instance_id()} if instance_id() else {})}
        try:
            status, data = _call("GET", f"{url}?{urlencode(query)}", client_auth=True)
        except OAuthError as error:
            log.warning("Platform sign-outs could not be fetched: %s", error)
            return ended
        if status == 404:
            return ended
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("events"), list):
            log.warning("Platform sign-outs could not be fetched: the portal answered %s", status)
            return ended
        latest = data.get("latest")
        if type(latest) is int and 0 <= latest < cursor:
            # The portal was restored from an older backup: its ids start over. Fetching sign-outs
            # again is harmless (they only reach sessions that started before them).
            log.info("Platform sign-out cursor %s is beyond the portal's latest %s; starting again", cursor, latest)
            cursor = 0
            _save_signout_cursor(cursor)
            continue
        events = [_valid_event(event, cursor) for event in data["events"]]
        if any(event is None for event in events):
            log.warning("Platform sign-outs refused: the portal sent an invalid event")
            return ended
        with db.transaction():
            for event_id, account_id, wiki_user_id, at in sorted(events):  # type: ignore[type-var]
                user_id = _signed_out_user(account_id, wiki_user_id)
                if user_id is not None:
                    ended += end_platform_sessions(user_id, at)
                cursor = max(cursor, event_id)
            _save_signout_cursor(cursor)
        if not data.get("more") or not events:
            break
    if ended:
        log.info("Platform sign-out ended %s session(s)", ended)
    return ended
