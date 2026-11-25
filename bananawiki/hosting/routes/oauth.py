"""OAuth 2.0 provider endpoints used by hosted wikis (``BW_PLATFORM_OAUTH_*`` URLs) and apps.

Apps (:mod:`..oauth_apps`) use ``/oauth/authorize``, ``/oauth/token``,
``/oauth/userinfo`` and ``/oauth/verify``, always with PKCE, and find them
in ``/.well-known/oauth-authorization-server`` (RFC 8414). The wiki-only
endpoints refuse their credentials.

Browser-facing: ``/oauth/authorize`` (consent) and ``/oauth/revoke``.
Server-to-server (called by the wiki, CSRF-exempt, never using the portal
session): ``/oauth/token``, ``/oauth/userinfo`` (alias ``/oauth/me``),
``/oauth/verify``, ``/oauth/link``, ``/oauth/unlink``, ``/oauth/link-status``
and ``/oauth/signouts``.

``prompt=create`` (OpenID "Initiating User Registration") without a portal session shows the portal's
sign-up form (every rule of ``/signup``; the sign-in form with a note while sign-up is closed), naming the
wiki or app, and then continues the request; with a session it changes nothing. A new account that must
confirm its address or wait for approval first gets those pages, with a link back to the wiki or app.
The metadata lists ``create`` while sign-up is not closed and says ``signup`` (``open``, ``invite`` or
``closed``).

``prompt=login`` (alone or with ``consent``) and ``max_age=N`` (0..86400 seconds since the portal sign-in)
ask for a fresh sign-in: the portal's sign-in page (password, then the two-step code when the account uses
one) even while a portal session exists; the request then continues without them. The code's token
response and userinfo carry ``auth_time`` and ``amr`` of that sign-in. An administrator acting as an
account (:mod:`..acting`, ``admin_app_signin`` on) signs in as it, after a click, without the consent page
(nothing is remembered for the account) and never with a fresh sign-in (``error=login_required``); the
code carries ``act`` and ``act_expires_at``, and each such sign-in is in the moderation log.

Every sign-in needs a click on the portal, like other "Sign in with" pages:
the first time (on the owner's own wikis too) the consent page says what the
wiki or app gets; once allowed, a short page asks to continue as the signed-in
account (``prompt=consent`` asks for the full consent again). Both offer
"Use another account", which signs out of the portal and comes back here after
the next sign-in. No code is ever issued for a GET.

Wiki authentication for ``token``, ``link``, ``unlink`` and ``link-status``:
HTTP Basic ``client_id:client_secret`` (preferred), or ``client_id`` and
``client_secret`` form/JSON fields (as 1.4 wikis send them). ``link`` and
``unlink`` also accept 1.4's JSON body with ``instance_id`` and
``client_secret``. ``link-status`` requires the wiki's credentials, and a wiki may only link a
hosting account that has signed in to it (holds a live access token).
Every endpoint answers 404 while platform sign-in is switched off. The
server-to-server endpoints are exempt from the account gates: they take
bearer or client credentials, never a portal session. They are not limited
per network either, since every call of a wiki comes from its own address:
they count failures and each wiki's calls instead (see :mod:`..limits`). While
sign-in is off they count against the global limit like any other path. A
network that sent too many wrong credentials or malformed requests is refused
before its next request is read, except by the bearer endpoints.

An account merge moves the source's links to the target here only, so the
wiki's own row still names the source. ``link-status`` therefore also answers
by ``account_id`` (the wiki then realigns its row), linking the same pair
again succeeds, and ``unlink`` also removes the link of a ``wiki_user_id``.
"""

from __future__ import annotations

import base64
import functools
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for

from ...core.numbers import ascii_int
from ...core.timeutil import sql_in
from ...core.web import allow_form_action, csrf_exempt, safe_next
from .. import accounts, acting, auth, events, limits, oauth, oauth_apps, signouts, urls
from ..db import db
from ..limits import rate_limit

bp = Blueprint("oauth", __name__)


def _enabled(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not oauth.enabled():
            abort(404)
        return view(*args, **kwargs)

    return wrapper


def _payload() -> dict[str, Any]:
    data = request.get_json(silent=True) if request.is_json else None
    return data if isinstance(data, dict) else request.form.to_dict()


def _client_credentials(data: dict[str, Any]) -> tuple[str, str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("basic "):
        try:
            client_id, _, secret = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
            return client_id, secret
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return str(data.get("client_id") or "").strip(), str(data.get("client_secret") or "").strip()


def _authenticated_instance(data: dict[str, Any]) -> dict[str, Any] | None:
    """The wiki whose credentials the request carries (1.4: instance_id + secret).

    Wrong credentials count against the network, which :func:`limits.server_to_server` refuses once over.
    """
    client_id, secret = _client_credentials(data)
    if not client_id and data.get("instance_id"):
        client_id = db.scalar("SELECT oauth_client_id FROM instances WHERE id = ?", (str(data["instance_id"]),)) or ""
    inst = oauth.authenticate_client(client_id, secret) if client_id and secret else None
    if inst and data.get("instance_id") and str(data["instance_id"]) != inst["id"]:
        inst = None
    if inst is None:
        limits.record(limits.BAD_CLIENT)
    return inst


def _authenticated_client(data: dict[str, Any]) -> dict[str, Any] | None:
    """The wiki or the app whose credentials the request carries (wrong ones count like :func:`_authenticated_instance`)."""
    client_id, secret = _client_credentials(data)
    if not client_id.startswith(oauth_apps.CLIENT_PREFIX):
        return _authenticated_instance(data)
    app = oauth_apps.authenticate(client_id, secret) if secret else None
    if app is None:
        limits.record(limits.BAD_CLIENT)
    return app


def _client(data: dict[str, Any], status: int, error: str) -> tuple[dict[str, Any] | None, Any]:
    """(the authenticated wiki, None), or (None, the answer refusing the request) in *status* and *error*."""
    inst = _authenticated_instance(data)
    if inst is None:
        return None, _json_error(status, error)
    if not limits.client_call(inst["oauth_client_id"]):
        return None, _json_error(429, "too_many_requests")
    return inst, None


def _json_error(code: int, error: str):
    response = jsonify({"ok": False, "error": error})
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/.well-known/oauth-authorization-server")
@auth.public
@auth.exempt(*auth.GATES)
@_enabled
def metadata():
    """Where apps find the endpoints (RFC 8414), and whether sign-up is ``open``, ``invite`` or ``closed``
    (``signup``; ``prompt=create`` is listed while it is not closed)."""
    signup = oauth.signup_state()
    base = urls.portal_base_url().rstrip("/")
    response = jsonify({
        "issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/oauth/userinfo", "scopes_supported": list(oauth_apps.SCOPES),
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "prompt_values_supported": ["login", "consent", *(["create"] if signup != "closed" else [])],
        "claims_supported": ["sub", "username", "preferred_username", "name", "email", "email_verified",
                             "created_at", "auth_time", "amr", "act", "act_expires_at"],
        "signup": signup,
    })
    # Short: clients hide their "Create an account" link from what this says about sign-up.
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


def _without_fresh_sign_in(path: str) -> str:
    """The authorize URL *path* without ``prompt=login``, ``prompt=create`` and ``max_age``: where the fresh
    sign-in or the sign-up returns to (``prompt=consent`` stays)."""
    base, _, query = path.partition("?")
    kept = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "max_age":
            continue
        if key == "prompt":
            value = " ".join(part for part in value.split() if part not in ("login", "create"))
            if not value:
                continue
        kept.append((key, value))
    return f"{base}?{urlencode(kept)}" if kept else base


def _max_age() -> int | None:
    """``max_age`` (0..86400), None when absent; 400 when it is something else."""
    raw = request.args.get("max_age")
    if raw is None:
        return None
    value = ascii_int(raw.strip(), 5) if raw.strip() else None
    if value is None or not 0 <= value <= oauth.MAX_AGE_LIMIT:
        abort(400)
    return value


def _sign_in_age() -> float | None:
    """Seconds since the interactive portal sign-in of this session (None when unknown)."""
    from ...core.timeutil import parse, utcnow

    started = parse((g.get("auth_session") or {}).get("created_at"))
    return (utcnow() - started).total_seconds() if started is not None else None


@bp.route("/oauth/authorize", methods=["GET", "POST"])
@auth.public
@_enabled
@rate_limit(20)
def authorize():
    args = request.args
    client_id = (args.get("client_id") or "").strip()
    redirect_uri = (args.get("redirect_uri") or "").strip()
    state = (args.get("state") or "")[:512]
    challenge = (args.get("code_challenge") or "").strip()
    method = (args.get("code_challenge_method") or ("S256" if challenge else "")).strip()
    inst = oauth.instance_for_client(client_id)
    app = oauth_apps.for_client(client_id) if inst is None else None
    if args.get("response_type") != "code" or (inst is None and app is None):
        abort(400)
    if inst is not None and not oauth.redirect_allowed(inst, redirect_uri):
        abort(400)
    # Apps must use PKCE: a code that leaks through the app's own pages is useless without the verifier.
    if app is not None and (not oauth_apps.redirect_allowed(app, redirect_uri) or not challenge):
        abort(400)
    if challenge and (method != "S256" or not oauth.valid_pkce_challenge(challenge)):
        abort(400)
    prompts = set((args.get("prompt") or "").split())
    max_age = _max_age()
    current = auth.current_account()
    if current is None:
        # The sign-in (or sign-up, for prompt=create) about to happen is fresh: the request continues
        # without asking for another one. With a portal session, prompt=create changes nothing.
        endpoint = "auth.signup" if "create" in prompts else "auth.login"
        return redirect(url_for(endpoint, next=_without_fresh_sign_in(request.full_path)))
    acting_row = auth.acting_log()
    if auth.impersonating() and (acting_row is None or not acting.app_signin_enabled()):
        abort(403)
    if auth.pending_gate(current, None) or not oauth.account_may_sign_in(current):
        abort(403)
    if app is not None and not oauth_apps.usable_by(app, current):
        return render_template("hosting/oauth_unavailable.html", app=app), 403
    scope = oauth_apps.requested_scopes(args.get("scope")) if app is not None else ""

    def answer(query: dict[str, str]):
        query = {**query, **({"state": state} if state else {})}
        return redirect(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(query)}")

    age = _sign_in_age()
    if "login" in prompts or (max_age is not None and (age is None or age > max_age)):
        if acting_row is not None:
            # The administrator cannot sign in again as the account: a client asking for that is told so.
            return answer({"error": "login_required"})
        return redirect(url_for("auth.login", again=1, next=_without_fresh_sign_in(request.full_path)))

    def issue():
        if acting_row is not None:
            # The administrator is not the one consenting: nothing is remembered for the account.
            events.record("account", current["id"], "account.acting_signin", acting_row["admin_account_id"],
                          app["name"] if app is not None else inst["subdomain"])  # type: ignore[index]
        elif app is not None:
            oauth_apps.remember_consent(current["id"], app["id"], scope)
        else:
            oauth.remember_consent(current["id"], inst["id"])  # type: ignore[index]
        auth_time, amr = oauth.sign_in_facts(g.get("auth_session"))
        return answer({"code": oauth.issue_code(client_id, current["id"], redirect_uri, challenge,
                                                "S256" if challenge else "", scope, auth_time=auth_time, amr=amr,
                                                act=acting_row)})

    if request.method == "POST":
        action = request.form.get("action")
        if action == "switch":
            if acting_row is not None:
                abort(403)  # the acting administrator stops acting on the portal instead
            # "Use another account": the login page brings the next account back to this request.
            auth.end_session()
            return redirect(url_for("auth.login", next=request.full_path))
        if action != "allow":
            return answer({"error": "access_denied"})
        return issue()
    # While acting the consent page is skipped (the short "Continue" page still needs the click).
    remembered = acting_row is not None or ("consent" not in prompts and (
        oauth_apps.consent_covers(current["id"], app["id"], scope) if app is not None
        else oauth.consent_remembered(current, inst)))  # type: ignore[arg-type]
    # The page is answered with a redirect to the wiki or app checked above.
    allow_form_action(redirect_uri)
    if app is not None:
        owner = accounts.get(app["account_id"])
        return render_template("hosting/oauth_consent.html", instance=None, app=app, scopes=scope.split(),
                               owner_name=owner["username"] if owner else "", remembered=remembered, acting=acting_row,
                               email_shared=bool("email" in scope.split() and oauth_apps.verified_email(current)))
    return render_template("hosting/oauth_consent.html", instance=inst, app=None, remembered=remembered,
                           acting=acting_row)


@bp.post("/oauth/token")
@auth.public
@auth.exempt(*auth.GATES)
@csrf_exempt
@_enabled
@limits.server_to_server
def token():
    """A code for an access token. Never limited for a wiki whose credentials and code are right: a wiki's
    visitors can make it send codes they made up (or none), which must not use up its users' sign-ins.

    Malformed requests count against the network like wrong credentials: no wiki sends them."""
    data = request.form.to_dict()
    if data.get("grant_type") != "authorization_code":
        limits.record(limits.BAD_CLIENT)
        return jsonify({"error": "unsupported_grant_type"}), 400
    client_id, secret = _client_credentials(data)
    if not client_id or not secret:
        limits.record(limits.BAD_CLIENT)
        return jsonify({"error": "invalid_request"}), 400
    inst = _authenticated_client(data)  # a wiki or an app
    if inst is None:
        return jsonify({"error": "invalid_client"}), 401
    code = (data.get("code") or "").strip()
    raw = oauth.redeem_code(code, client_id, (data.get("redirect_uri") or "").strip(),
                            (data.get("code_verifier") or "").strip()) if code else None
    if raw is None:
        error = "invalid_grant" if code else "invalid_request"
        if not limits.failure(limits.BAD_GRANT, limits.BAD_GRANT_PER_MINUTE, key=inst["id"]):
            return jsonify({"error": error}), 429
        return jsonify({"error": error}), 400
    issued = oauth.token_row(raw) or {}
    response = jsonify({"access_token": raw, "token_type": "Bearer", "expires_in": _expires_in(issued),
                        "scope": issued.get("scope") or oauth.SCOPE, **oauth.sign_in_claims(issued)})
    response.headers["Cache-Control"] = "no-store"
    return response


def _expires_in(row: dict[str, Any]) -> int:
    """Seconds the token lives (less than an hour when it was issued while acting, which ends sooner)."""
    from ...core.timeutil import parse, utcnow

    expires = parse(row.get("expires_at")) if row.get("act_expires_at") else None
    if expires is None:
        return oauth.TOKEN_SECONDS
    return max(0, min(oauth.TOKEN_SECONDS, int((expires - utcnow()).total_seconds())))


def _bearer() -> tuple[dict[str, Any] | None, Any]:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    row = oauth.token_row(value.strip()) if scheme.lower() == "bearer" else None
    if row is None:
        # Expired tokens are ordinary (1.4 wikis verify theirs): a valid one is never refused for them. Over the
        # limit, a refusal costs a read only.
        if not limits.failure(limits.BAD_TOKEN, limits.BAD_TOKEN_PER_MINUTE):
            return None, (jsonify({"error": "too_many_requests"}), 429)
        return None, (jsonify({"error": "invalid_token"}), 401)
    if not limits.client_call(row["client_id"]):
        return None, (jsonify({"error": "too_many_requests"}), 429)
    return row, None


def _token_account(row: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, Any]:
    """(the account a live token is for, the app holding it or None, None), or (None, None, the refusal)."""
    account = accounts.get(row["account_id"])
    if account is None or account["deleted_at"]:
        return None, None, (jsonify({"error": "account_not_found"}), 404)
    if not oauth.account_may_sign_in(account):
        return None, None, (jsonify({"error": oauth.refusal(account)}), 403)
    if not acting.sign_in_valid(row):
        return None, None, (jsonify({"error": "invalid_token"}), 401)  # the acting it was issued in ended
    app = oauth_apps.for_client(row["client_id"])
    if app is not None and not oauth_apps.token_usable(app, account):
        return None, None, (jsonify({"error": "invalid_token"}), 401)  # suspended since, or its owner was
    return account, app, None


@bp.get("/oauth/userinfo")
@bp.get("/oauth/me")
@auth.public
@auth.exempt(*auth.GATES)
@_enabled
@limits.server_to_server(bearer=True)
def userinfo():
    row, refusal = _bearer()
    if row is None:
        return refusal
    account, app, refusal = _token_account(row)
    if account is None:
        return refusal
    if app is None:
        response = jsonify({"sub": account["id"], "username": account["username"],
                            "preferred_username": account["username"],
                            "is_admin": bool(account["is_admin"]) and not auth.admin_mfa_missing(account),
                            "created_at": account["created_at"], **oauth.sign_in_claims(row)})
        response.headers["Cache-Control"] = "no-store"
        return response
    # Apps are never told who administers the platform, and get an address only when they asked for it.
    info = {"sub": account["id"], "username": account["username"], "preferred_username": account["username"],
            "name": account["username"], "created_at": account["created_at"]}
    email = oauth_apps.verified_email(account) if "email" in (row["scope"] or "").split() else ""
    if email:
        info.update(email=email, email_verified=True)
    response = jsonify({**info, **oauth.sign_in_claims(row)})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/oauth/verify")
@auth.public
@auth.exempt(*auth.GATES)
@_enabled
@limits.server_to_server(bearer=True)
def verify():
    row, refusal = _bearer()
    if row is None:
        return refusal
    account, _app, refusal = _token_account(row)
    if account is None:
        return refusal
    return jsonify({"active": True, "sub": row["account_id"], "client_id": row["client_id"],
                    "exp": row["expires_at"], "scope": row["scope"]})


@bp.get("/oauth/link-status")
@auth.public
@auth.exempt(*auth.GATES)
@_enabled
@limits.server_to_server
def link_status():
    inst, refusal = _client(request.args.to_dict(), 401, "invalid_client")
    if inst is None:
        return refusal
    wiki_user_id = (request.args.get("wiki_user_id") or "").strip()
    account_id = (request.args.get("account_id") or "").strip()
    if wiki_user_id:
        row = db.one("SELECT * FROM hosting_oauth_account_links WHERE instance_id = ? AND wiki_user_id = ? "
                     "ORDER BY id DESC LIMIT 1", (inst["id"], wiki_user_id))
    elif account_id:
        row = db.one("SELECT * FROM hosting_oauth_account_links WHERE instance_id = ? AND account_id = ?",
                     (inst["id"], account_id))
    else:
        return _json_error(400, "missing_parameters")
    if row is None:
        return jsonify({"linked": False})
    return jsonify({"linked": True, "hosting_account_id": row["account_id"], "hosting_username": row["wiki_username"],
                    "wiki_user_id": row["wiki_user_id"]})


@bp.post("/oauth/link")
@auth.public
@auth.exempt(*auth.GATES)
@csrf_exempt
@_enabled
@limits.server_to_server
def link():
    data = _payload()
    inst, refusal = _client(data, 403, "invalid_client_secret")
    if inst is None:
        return refusal
    wiki_user_id = str(data.get("wiki_user_id") or "").strip()[:128]
    wiki_username = str(data.get("wiki_username") or "").strip()[:200]
    access = oauth.token_row(str(data.get("access_token") or "").strip()) if data.get("access_token") else None
    if access and access.get("act_account_id"):
        return _json_error(403, "acting_administrator")  # links are the person's own, never made while acting
    account_id = access["account_id"] if access and access["client_id"] == inst["oauth_client_id"] else \
        str(data.get("account_id") or "").strip()
    if not account_id or not wiki_user_id:
        return _json_error(400, "missing_parameters")
    with db.transaction():
        if not oauth.account_authorized_client(account_id, inst["oauth_client_id"]):
            return _json_error(403, "account_not_authorized")
        linked = db.scalar("SELECT wiki_user_id FROM hosting_oauth_account_links WHERE instance_id = ? "
                           "AND account_id = ?", (inst["id"], account_id))
        if linked is not None and linked != wiki_user_id:
            return _json_error(409, "This hosting account is already linked to another wiki user on this instance.")
        if linked is not None:
            db.execute("UPDATE hosting_oauth_account_links SET wiki_username = ? WHERE instance_id = ? "
                       "AND account_id = ?", (wiki_username, inst["id"], account_id))
            return jsonify({"ok": True})
        # The wiki links a wiki user to one hosting account: a link of it to another one here is stale.
        db.execute("DELETE FROM hosting_oauth_account_links WHERE instance_id = ? AND wiki_user_id = ?",
                   (inst["id"], wiki_user_id))
        db.insert("hosting_oauth_account_links", {"instance_id": inst["id"], "account_id": account_id,
                                                  "wiki_user_id": wiki_user_id, "wiki_username": wiki_username})
        # A wiki may unlink and link again at will: one entry a day per account and wiki keeps the log bounded.
        if not events.recorded_since("account", account_id, "oauth.linked", inst["subdomain"], sql_in(days=-1)):
            events.record("account", account_id, "oauth.linked", account_id, inst["subdomain"])
    return jsonify({"ok": True})


@bp.post("/oauth/unlink")
@auth.public
@auth.exempt(*auth.GATES)
@csrf_exempt
@_enabled
@limits.server_to_server
def unlink():
    data = _payload()
    inst, refusal = _client(data, 403, "invalid_client_secret")
    if inst is None:
        return refusal
    account_id = str(data.get("account_id") or "").strip()
    wiki_user_id = str(data.get("wiki_user_id") or "").strip()[:128]
    if not account_id and not wiki_user_id:
        return _json_error(400, "missing_parameters")
    for column, value in (("account_id", account_id), ("wiki_user_id", wiki_user_id)):
        if value:
            db.execute(f"DELETE FROM hosting_oauth_account_links WHERE instance_id = ? AND {column} = ?",
                       (inst["id"], value))
    return jsonify({"ok": True})


@bp.get("/oauth/signouts")
@auth.public
@auth.exempt(*auth.GATES)
@_enabled
@limits.server_to_server
def signout_events():
    """The sign-outs a wiki must carry out, after the id it got last (``after``), oldest first.

    ``latest`` is the wiki's newest event id: a wiki whose cursor is beyond it
    (a portal restored from an older backup) starts again from 0. Each event
    asks the wiki to end the platform sessions of the wiki account linked to
    ``account_id`` that started at or before ``at``.
    """
    inst, refusal = _client(request.args.to_dict(), 401, "invalid_client")
    if inst is None:
        return refusal
    after = ascii_int(request.args.get("after") or "0", 19)
    if after is None or after < 0 or after >= 2**63:
        return _json_error(400, "invalid_cursor")
    rows, latest = signouts.pending(inst["id"], after)
    response = jsonify({"events": [{"id": row["id"], "account_id": row["account_id"],
                                    "wiki_user_id": row["wiki_user_id"], "reason": row["reason"],
                                    "at": row["created_at"]} for row in rows],
                        "latest": latest, "more": len(rows) >= signouts.PAGE_SIZE})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/oauth/revoke")
@auth.exempt(*auth.GATES)  # revoking only takes access away: never held up by an account state
@_enabled
@rate_limit(10)
def revoke():
    """Sign out of every wiki: the wikis end the platform sessions and every access token goes."""
    current = auth.current_account()
    if auth.impersonating():
        abort(403)
    oauth.sign_out_everywhere(current["id"])  # type: ignore[index]
    auth.flash_t("hosting.oauth.revoked", "success")
    return redirect(safe_next(url_for("account.account"), request.form.get("next")))


__all__ = ["bp"]
