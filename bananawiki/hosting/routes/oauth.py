"""OAuth 2.0 provider endpoints used by hosted wikis (``BW_PLATFORM_OAUTH_*`` URLs).

Browser-facing: ``/oauth/authorize`` (consent) and ``/oauth/revoke``.
Server-to-server (called by the wiki, CSRF-exempt, never using the portal
session): ``/oauth/token``, ``/oauth/userinfo`` (alias ``/oauth/me``),
``/oauth/verify``, ``/oauth/link``, ``/oauth/unlink`` and ``/oauth/link-status``.

Wiki authentication for ``token``, ``link``, ``unlink`` and ``link-status``:
HTTP Basic ``client_id:client_secret`` (preferred), or ``client_id`` and
``client_secret`` form/JSON fields (1.4). ``link`` and ``unlink`` also accept
1.4's JSON body with ``instance_id`` and ``client_secret``. Unlike 1.4,
``link-status`` requires the wiki's credentials, and a wiki may only link a
hosting account that has signed in to it (holds a live access token).
Every endpoint answers 404 while platform sign-in is switched off.

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
from urllib.parse import urlencode

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from ...core.web import allow_form_action, csrf_exempt
from .. import accounts, auth, events, oauth
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
    """The wiki whose credentials the request carries (1.4: instance_id + secret)."""
    client_id, secret = _client_credentials(data)
    if not client_id and data.get("instance_id"):
        client_id = db.scalar("SELECT oauth_client_id FROM instances WHERE id = ?", (str(data["instance_id"]),)) or ""
    inst = oauth.authenticate_client(client_id, secret) if client_id and secret else None
    if inst and data.get("instance_id") and str(data["instance_id"]) != inst["id"]:
        return None
    return inst


def _json_error(code: int, error: str):
    response = jsonify({"ok": False, "error": error})
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


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
    if args.get("response_type") != "code" or inst is None or not oauth.redirect_allowed(inst, redirect_uri):
        abort(400)
    if challenge and (method != "S256" or not oauth.valid_pkce_challenge(challenge)):
        abort(400)
    current = auth.current_account()
    if current is None:
        return redirect(url_for("auth.login", next=request.full_path))
    if auth.pending_gate(current, None) or not oauth.account_may_sign_in(current) or auth.impersonating():
        abort(403)
    if request.method == "POST":
        if request.form.get("action") != "allow":
            query = {"error": "access_denied", **({"state": state} if state else {})}
            return redirect(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(query)}")
        code = oauth.issue_code(client_id, current["id"], redirect_uri, challenge, "S256" if challenge else "")
        query = {"code": code, **({"state": state} if state else {})}
        return redirect(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(query)}")
    # The consent form is answered with a redirect to the wiki checked above.
    allow_form_action(redirect_uri)
    return render_template("hosting/oauth_consent.html", instance=inst)


@bp.post("/oauth/token")
@auth.public
@csrf_exempt
@_enabled
@rate_limit(20)
def token():
    data = request.form.to_dict()
    if data.get("grant_type") != "authorization_code":
        return jsonify({"error": "unsupported_grant_type"}), 400
    client_id, secret = _client_credentials(data)
    if not client_id or not secret or not data.get("code"):
        return jsonify({"error": "invalid_request"}), 400
    if oauth.authenticate_client(client_id, secret) is None:
        return jsonify({"error": "invalid_client"}), 401
    raw = oauth.redeem_code(data["code"].strip(), client_id, (data.get("redirect_uri") or "").strip(),
                            (data.get("code_verifier") or "").strip())
    if raw is None:
        return jsonify({"error": "invalid_grant"}), 400
    response = jsonify({"access_token": raw, "token_type": "Bearer", "expires_in": oauth.TOKEN_SECONDS,
                        "scope": oauth.SCOPE})
    response.headers["Cache-Control"] = "no-store"
    return response


def _bearer() -> tuple[dict[str, Any] | None, Any]:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    row = oauth.token_row(value.strip()) if scheme.lower() == "bearer" else None
    if row is None:
        return None, (jsonify({"error": "invalid_token"}), 401)
    return row, None


@bp.get("/oauth/userinfo")
@bp.get("/oauth/me")
@auth.public
@_enabled
@rate_limit(60)
def userinfo():
    row, refusal = _bearer()
    if row is None:
        return refusal
    account = accounts.get(row["account_id"])
    if account is None or account["deleted_at"]:
        return jsonify({"error": "account_not_found"}), 404
    if not oauth.account_may_sign_in(account):
        return jsonify({"error": "account_suspended"}), 403
    return jsonify({"sub": account["id"], "username": account["username"], "preferred_username": account["username"],
                    "is_admin": bool(account["is_admin"]), "created_at": account["created_at"]})


@bp.get("/oauth/verify")
@auth.public
@_enabled
@rate_limit(60)
def verify():
    row, refusal = _bearer()
    if row is None:
        return refusal
    account = accounts.get(row["account_id"])
    if account is None or account["deleted_at"]:
        return jsonify({"error": "account_not_found"}), 404
    if not oauth.account_may_sign_in(account):
        return jsonify({"error": "account_suspended"}), 403
    return jsonify({"active": True, "sub": row["account_id"], "client_id": row["client_id"],
                    "exp": row["expires_at"], "scope": row["scope"]})


@bp.get("/oauth/link-status")
@auth.public
@_enabled
@rate_limit(60)
def link_status():
    inst = _authenticated_instance(request.args.to_dict())
    if inst is None:
        return _json_error(401, "invalid_client")
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
@csrf_exempt
@_enabled
@rate_limit(30)
def link():
    data = _payload()
    inst = _authenticated_instance(data)
    if inst is None:
        return _json_error(403, "invalid_client_secret")
    wiki_user_id = str(data.get("wiki_user_id") or "").strip()[:128]
    wiki_username = str(data.get("wiki_username") or "").strip()[:200]
    access = oauth.token_row(str(data.get("access_token") or "").strip()) if data.get("access_token") else None
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
        events.record("account", account_id, "oauth.linked", account_id, inst["subdomain"])
    return jsonify({"ok": True})


@bp.post("/oauth/unlink")
@auth.public
@csrf_exempt
@_enabled
@rate_limit(30)
def unlink():
    data = _payload()
    inst = _authenticated_instance(data)
    if inst is None:
        return _json_error(403, "invalid_client_secret")
    account_id = str(data.get("account_id") or "").strip()
    wiki_user_id = str(data.get("wiki_user_id") or "").strip()[:128]
    if not account_id and not wiki_user_id:
        return _json_error(400, "missing_parameters")
    for column, value in (("account_id", account_id), ("wiki_user_id", wiki_user_id)):
        if value:
            db.execute(f"DELETE FROM hosting_oauth_account_links WHERE instance_id = ? AND {column} = ?",
                       (inst["id"], value))
    return jsonify({"ok": True})


@bp.post("/oauth/revoke")
@_enabled
@rate_limit(10)
def revoke():
    oauth.revoke_account_tokens(auth.current_account()["id"])  # type: ignore[index]
    auth.flash_t("hosting.oauth.revoked", "success")
    return redirect(url_for("account.account"))


__all__ = ["bp"]
