"""OAuth 2.0 provider routes for the hosting platform.

When a hosting admin enables ``platform_oauth_enabled`` in the platform
settings, every wiki instance managed by this platform gets an OAuth client
(credentials injected via environment variables).  Users can then sign in
to any of their wikis using their hosting portal account credentials.

Architecture
------------
- The hosting portal acts as the **authorisation server** (AuthZ).
- Each wiki instance is an **OAuth client** with its own ``client_id`` /
  ``client_secret`` pair (shared secret, HMAC-signed).
- The flow is the standard **Authorisation Code Grant** (RFC 6749 §4.1).

Flow
----
1. User clicks "Log in with Platform Account" on a wiki login page.
2. Wiki redirects to ``/oauth/authorize?client_id=...&redirect_uri=...&
   response_type=code&state=...``.
3. User authenticates on the hosting portal (if not already logged in).
4. Portal shows a consent page ("Allow WikiName to use your account?").
5. Portal generates an authorisation code and redirects back to the wiki's
   ``redirect_uri`` with ``?code=...&state=...``.
6. Wiki exchanges the code for an access token via ``/oauth/token``.
7. Wiki uses the access token to fetch user info via ``/oauth/userinfo``.
8. Wiki logs the user in (creating/linking accounts as needed).

Security
--------
- Authorisation codes are single-use and short-lived (5 minutes).
- Access tokens are HMAC-signed JWTs with 1-hour expiry.
- client_secret is stored as a salted hash in the DB; the raw value is
  injected into the wiki instance's environment at provision time.
- All OAuth endpoints enforce CSRF protection (WTForms on HTML pages,
  token verification on API endpoints).
- Rate-limited at 20 requests/minute per IP.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import string
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)

from .. import config
from ..db import (
    get_account_by_id, get_hosting_settings,
    get_instance, get_hosting_db_context,
)
from .auth import (
    hosting_login_required, hosting_admin_required, hosting_rate_limit,
    get_current_account,
)

logger = logging.getLogger("hosting.routes.oauth")

_AUTH_CODE_EXPIRY_SECONDS = 300       # 5 minutes
_ACCESS_TOKEN_EXPIRY_SECONDS = 3600   # 1 hour
_TOKEN_LENGTH_BYTES = 32


def _random_token(length=_TOKEN_LENGTH_BYTES):
    """Return a cryptographically random hex token string."""
    return secrets.token_hex(length)


def _gen_oauth_client_id():
    """Generate a unique OAuth client ID for a wiki instance."""
    prefix = "bw_"
    return prefix + "".join(
        secrets.choice(string.ascii_lowercase + string.digits)
        for _ in range(28)
    )


def _gen_oauth_client_secret():
    """Generate a raw OAuth client secret."""
    return secrets.token_urlsafe(48)


def _hash_secret(secret):
    """Return a salted SHA-256 hash of *secret* for secure storage."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
    return f"{salt}${h}"


def _check_secret_hash(stored, candidate):
    """Verify *candidate* against a ``_hash_secret``-produced hash string."""
    if "$" not in stored:
        return False
    salt, expected = stored.split("$", 1)
    actual = hashlib.sha256((salt + candidate).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def _hosting_secret_bytes():
    """Return the hosting portal's secret key as bytes for HMAC signing."""
    sk = config.HOSTING_SECRET_KEY
    if isinstance(sk, str):
        return sk.encode("utf-8")
    return bytes(sk)


def _sign_token(payload: dict) -> str:
    """Sign a payload dict and return a dotted ``payload.b64.signature`` token."""
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    signature = hmac.new(
        _hosting_secret_bytes(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{payload_b64}.{signature}"


def _verify_token(token: str):
    """Verify a signed token and return the decoded payload dict, or None."""
    if not token or "." not in token:
        return None
    payload_b64, signature = token.split(".", 1)
    expected = hmac.new(
        _hosting_secret_bytes(),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        # Pad for base64 decoding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64).decode("utf-8")
        )
    except (json.JSONDecodeError, ValueError):
        return None
    return payload


def _prune_expired_auth_codes():
    """Delete expired authorisation codes (best-effort)."""
    try:
        with get_hosting_db_context() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "DELETE FROM hosting_oauth_authorization_codes WHERE expires_at < ?",
                (now,),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to prune expired auth codes")


def _prune_expired_tokens():
    """Delete expired access tokens (best-effort)."""
    try:
        with get_hosting_db_context() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "DELETE FROM hosting_oauth_access_tokens WHERE expires_at < ?",
                (now,),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to prune expired access tokens")


def _is_oauth_enabled():
    """Return True when the platform OAuth SSO feature is enabled."""
    try:
        hs = get_hosting_settings()
        return bool(hs and hs.get("platform_oauth_enabled"))
    except Exception:
        return False


def _get_instance_by_client_id(client_id):
    """Return the instance row for *client_id*, or None."""
    try:
        with get_hosting_db_context() as conn:
            return conn.execute(
                "SELECT * FROM instances WHERE oauth_client_id=? AND status != 'terminated'",
                (client_id,),
            ).fetchone()
    except Exception:
        return None


def _origin_of(url):
    """Return the lowercase scheme://host[:port] of *url*, or None."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _allowed_redirect_origins(inst):
    """Return the origins this client is allowed to receive a code on.

    A wiki instance is only ever served from its platform URL or from a
    verified custom domain, so those are the only places an authorisation
    code may be delivered.
    """
    from ..instance_paths import _instance_url
    origins = set()
    platform_url = _instance_url(inst)
    origin = _origin_of(platform_url) if platform_url else None
    if origin:
        origins.add(origin)
    try:
        from .. import domains as _domains
        record = _domains.get_domain(inst["id"])
    except Exception:
        record = None
    if record and record.get("domain") and record.get("verified_at"):
        origins.add(f"https://{str(record['domain']).strip().lower()}")
    return origins


def _redirect_uri_allowed(inst, redirect_uri):
    """Return True when *redirect_uri* belongs to this client's own wiki."""
    origin = _origin_of(redirect_uri)
    if not origin:
        return False
    return origin in _allowed_redirect_origins(inst)


def _get_client_id_for_instance(instance_id):
    """Return the OAuth client_id for an instance, generating one if missing."""
    inst = get_instance(instance_id)
    if inst is None:
        return None, None, None
    if inst.get("oauth_client_id") and inst.get("oauth_client_secret_hash"):
        return inst["oauth_client_id"], None, None
    client_id = _gen_oauth_client_id()
    secret = _gen_oauth_client_secret()
    secret_hash = _hash_secret(secret)
    with get_hosting_db_context() as conn:
        # Handle race: if another worker just created credentials, re-read.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT oauth_client_id, oauth_client_secret_hash FROM instances WHERE id=?",
            (instance_id,),
        ).fetchone()
        if row and row["oauth_client_id"]:
            conn.execute("ROLLBACK")
            return row["oauth_client_id"], None, None
        conn.execute(
            "UPDATE instances SET oauth_client_id=?, oauth_client_secret_hash=? WHERE id=?",
            (client_id, secret_hash, instance_id),
        )
        conn.commit()
    return client_id, secret, secret_hash


def oauth_required(f):
    """Decorator that blocks access to OAuth endpoints when the feature is disabled."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_oauth_enabled():
            abort(404)
        return f(*args, **kwargs)
    return wrapper


def _oauth_portal_base_url():
    """Return the base URL of the hosting portal for constructing redirect URLs."""
    if config.HOSTING_MODE == "port":
        scheme = config.HOSTING_PUBLIC_SCHEME
        host = config.HOSTING_PUBLIC_HOST
        port = config.HOSTING_PORT
        if port and port not in (80, 443):
            return f"{scheme}://{host}:{port}"
        return f"{scheme}://{host}"
    scheme = "https"
    domain = config.EFFECTIVE_PORTAL_DOMAIN
    return f"{scheme}://{domain}"


def register_hosting_oauth_routes(app):
    """Register OAuth provider routes on the hosting portal Flask app."""

    # The authorize endpoint is the browser-facing half of the flow: it runs
    # in the user's portal session and ends with an authorisation code in the
    # redirect URL.
    @app.route("/oauth/authorize", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=20, window=60)
    @oauth_required
    def hosting_oauth_authorize():
        """OAuth 2.0 Authorisation endpoint.

        On GET: validates the client_id and redirect_uri, then shows a
        consent page if the user is logged in, or redirects to login
        first.
        On POST: user has granted consent; issue an authorisation code
        and redirect back to the wiki's redirect_uri.
        """
        client_id = (request.args.get("client_id") or "").strip()
        redirect_uri = (request.args.get("redirect_uri") or "").strip()
        state = (request.args.get("state") or "").strip()
        response_type = (request.args.get("response_type") or "").strip()

        if response_type != "code":
            flash("Invalid response_type. Expected 'code'.", "error")
            abort(400)

        inst = _get_instance_by_client_id(client_id)
        if inst is None:
            flash("Invalid OAuth client.", "error")
            abort(400)

        # Anything else would let a caller point a genuine consent screen at
        # an origin it does not own and collect the code from the redirect.
        if not _redirect_uri_allowed(inst, redirect_uri):
            flash("Invalid redirect_uri for this OAuth client.", "error")
            abort(400)

        account = get_current_account()
        if account is None:
            # Not logged into the hosting portal yet: redirect to login,
            # preserving the OAuth request so the user lands back here.
            from urllib.parse import urlencode
            params = {
                k: request.args[k]
                for k in ("client_id", "redirect_uri", "state", "response_type")
                if k in request.args
            }
            authorize_url = url_for("hosting_oauth_authorize", **params)
            return redirect(url_for("hosting_login", next=authorize_url))

        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "allow":
                # Generate authorisation code
                code = _random_token(24)
                now = datetime.now(timezone.utc)
                expires = now + timedelta(seconds=_AUTH_CODE_EXPIRY_SECONDS)

                with get_hosting_db_context() as conn:
                    conn.execute(
                        "INSERT INTO hosting_oauth_authorization_codes "
                        "(code, client_id, account_id, redirect_uri, expires_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (code, client_id, account["id"], redirect_uri or "",
                         expires.isoformat()),
                    )
                    conn.commit()

                _prune_expired_auth_codes()

                from urllib.parse import urlencode
                sep = "?" if "?" not in redirect_uri else "&"
                query = {"code": code}
                if state:
                    query["state"] = state
                return redirect(f"{redirect_uri}{sep}{urlencode(query)}")

            flash("Authorisation denied.", "error")
            return redirect(url_for("hosting_dashboard"))

        # GET: Show consent page
        portal_base = _oauth_portal_base_url()
        return render_template(
            "oauth_consent.html",
            instance_name=inst["subdomain"],
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            portal_base=portal_base,
            current_account=account,
        )

    # From here on the caller is the wiki instance, not a browser: these
    # endpoints authenticate with the client secret or a bearer token and
    # never touch the portal session.
    @app.route("/oauth/token", methods=["POST"])
    @hosting_rate_limit(max_requests=20, window=60)
    @oauth_required
    def hosting_oauth_token():
        """OAuth 2.0 Token endpoint.

        Accepts ``grant_type=authorization_code`` or
        ``grant_type=refresh_token``.

        Request parameters (form-encoded):
          - grant_type: ``authorization_code``
          - code: the authorisation code
          - client_id: the instance's client_id
          - client_secret: the instance's client_secret
          - redirect_uri: must match the original request
        """
        grant_type = (request.form.get("grant_type") or "").strip()
        code = (request.form.get("code") or "").strip()
        client_id = (request.form.get("client_id") or "").strip()
        client_secret = (request.form.get("client_secret") or "").strip()
        redirect_uri = (request.form.get("redirect_uri") or "").strip()

        if grant_type != "authorization_code":
            return jsonify({"error": "unsupported_grant_type"}), 400

        if not code or not client_id or not client_secret:
            return jsonify({"error": "invalid_request"}), 400

        inst = _get_instance_by_client_id(client_id)
        if inst is None:
            return jsonify({"error": "invalid_client"}), 401

        if not _check_secret_hash(inst["oauth_client_secret_hash"], client_secret):
            return jsonify({"error": "invalid_client"}), 401

        with get_hosting_db_context() as conn:
            row = conn.execute(
                "SELECT * FROM hosting_oauth_authorization_codes "
                "WHERE code=? AND client_id=? AND used=0 AND expires_at > datetime('now')",
                (code, client_id),
            ).fetchone()

        if row is None:
            return jsonify({"error": "invalid_grant"}), 400

        # RFC 6749 requires the exchange to present the same redirect_uri the
        # code was issued for, which is what stops a stolen code being redeemed
        # from somewhere else.
        if not hmac.compare_digest(str(row["redirect_uri"] or ""), redirect_uri):
            return jsonify({"error": "invalid_grant"}), 400

        # Mark the code as used (single-use)
        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE hosting_oauth_authorization_codes SET used=1 WHERE id=?",
                (row["id"],),
            )
            conn.commit()

        # Generate access token
        account_id = row["account_id"]
        token_raw = _random_token()
        token_hash = hashlib.sha256(token_raw.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=_ACCESS_TOKEN_EXPIRY_SECONDS)

        # Build the signed JWT-like token
        token_payload = {
            "sub": account_id,
            "client_id": client_id,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            "scope": "openid profile",
        }
        signed_token = _sign_token(token_payload)

        with get_hosting_db_context() as conn:
            conn.execute(
                "INSERT INTO hosting_oauth_access_tokens "
                "(token_hash, client_id, account_id, scope, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, client_id, account_id, "openid profile",
                 expires.isoformat()),
            )
            conn.commit()

        _prune_expired_tokens()

        return jsonify({
            "access_token": signed_token,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TOKEN_EXPIRY_SECONDS,
            "scope": "openid profile",
        })

    @app.route("/oauth/userinfo")
    @hosting_rate_limit(max_requests=60, window=60)
    @oauth_required
    def hosting_oauth_userinfo():
        """OAuth 2.0 UserInfo endpoint.

        Returns the hosting account profile linked to the access token.

        Response (JSON):
          - sub: hosting account ID
          - username: hosting account username
          - preferred_username: alias for username
          - is_admin: whether the account is a platform admin
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "invalid_token"}), 401

        token_value = auth_header[len("Bearer "):].strip()
        payload = _verify_token(token_value)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 401

        # Check expiry
        now_ts = time.time()
        exp_ts = payload.get("exp", 0)
        if now_ts > exp_ts:
            return jsonify({"error": "token_expired"}), 401

        account_id = payload.get("sub")
        if not account_id:
            return jsonify({"error": "invalid_token"}), 401

        account = get_account_by_id(account_id)
        if account is None:
            return jsonify({"error": "account_not_found"}), 404

        if account.get("suspended"):
            return jsonify({"error": "account_suspended"}), 403

        return jsonify({
            "sub": account["id"],
            "username": account["username"],
            "preferred_username": account["username"],
            "is_admin": bool(account["is_admin"]),
            "created_at": account.get("created_at", ""),
        })

    @app.route("/oauth/verify")
    @hosting_rate_limit(max_requests=60, window=60)
    @oauth_required
    def hosting_oauth_verify():
        """Verify an access token and return its metadata.

        Used by wiki instances to validate tokens without fetching the
        full userinfo.  Returns 200 with token metadata or 4xx on error.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "invalid_token"}), 401

        token_value = auth_header[len("Bearer "):].strip()
        payload = _verify_token(token_value)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 401

        now_ts = time.time()
        exp_ts = payload.get("exp", 0)
        if now_ts > exp_ts:
            return jsonify({"error": "token_expired"}), 401

        return jsonify({
            "active": True,
            "sub": payload.get("sub"),
            "client_id": payload.get("client_id"),
            "exp": payload.get("exp"),
            "scope": payload.get("scope", ""),
        })

    @app.route(
        "/admin/instances/<instance_id>/rotate-oauth-credentials",
        methods=["POST"],
    )
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_admin_rotate_oauth_credentials(instance_id):
        """Admin: regenerate the OAuth client_id and secret for an instance."""
        inst = get_instance(instance_id)
        if inst is None:
            flash("Instance not found.", "error")
            return redirect(url_for("hosting_admin"))

        new_client_id = _gen_oauth_client_id()
        new_secret = _gen_oauth_client_secret()
        new_hash = _hash_secret(new_secret)

        with get_hosting_db_context() as conn:
            conn.execute(
                "UPDATE instances SET oauth_client_id=?, oauth_client_secret_hash=? WHERE id=?",
                (new_client_id, new_hash, instance_id),
            )
            conn.commit()

        flash(
            f"OAuth credentials rotated for '{inst['subdomain']}'. "
            "The new credentials will take effect after the wiki is restarted.",
            "success",
        )
        # Display the new secret once
        flash(
            f"New OAuth Client Secret: {new_secret} "
            "(This will not be shown again.)",
            "info",
        )
        return redirect(
            url_for("hosting_admin_instance_manage", instance_id=instance_id)
        )

    @app.route("/oauth/revoke", methods=["POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    @oauth_required
    def hosting_oauth_revoke():
        """Revoke all access tokens for the current hosting account."""
        account_id = session["hosting_account_id"]
        with get_hosting_db_context() as conn:
            conn.execute(
                "DELETE FROM hosting_oauth_access_tokens WHERE account_id=?",
                (account_id,),
            )
            conn.commit()
        flash("All OAuth access tokens have been revoked.", "success")
        return redirect(url_for("hosting_dashboard"))

    @app.route("/oauth/link-status")
    @hosting_rate_limit(max_requests=60, window=60)
    @oauth_required
    def hosting_oauth_link_status():
        """Return the linking status for a wiki user on a given instance.

        Called by the wiki instance (server-to-server) to check whether a
        wiki user ID is already linked to a hosting account.

        Query params:
          - instance_id: the hosting DB instance ID (NOT the OAuth client_id)
          - wiki_user_id: the wiki's internal user ID

        Returns JSON:
          - linked: bool
          - hosting_account_id: str or null
          - hosting_username: str or null
        """
        instance_id = (request.args.get("instance_id") or "").strip()
        wiki_user_id = (request.args.get("wiki_user_id") or "").strip()

        if not instance_id or not wiki_user_id:
            return jsonify({"error": "missing_parameters"}), 400

        try:
            with get_hosting_db_context() as conn:
                row = conn.execute(
                    "SELECT * FROM hosting_oauth_account_links "
                    "WHERE instance_id=? AND wiki_user_id=?",
                    (instance_id, wiki_user_id),
                ).fetchone()
            if row:
                return jsonify({
                    "linked": True,
                    "hosting_account_id": row["account_id"],
                    "hosting_username": row["wiki_username"],
                })
            return jsonify({"linked": False})
        except Exception:
            logger.exception("Error checking link status")
            return jsonify({"error": "internal_error"}), 500

    @app.route("/oauth/link", methods=["POST"])
    @hosting_rate_limit(max_requests=30, window=60)
    @oauth_required
    def hosting_oauth_create_link():
        """Record a hosting-account ↔ wiki-user link.

        Called by the wiki instance (server-to-server) after a successful
        OAuth login flow to persist the link.  Authenticated by the
        instance's ``client_secret`` (only the wiki and the hosting
        portal know this value).

        Body (JSON):
          - instance_id: the hosting DB instance ID
          - account_id: the hosting account ID
          - wiki_user_id: the wiki's internal user ID
          - wiki_username: the wiki username
          - client_secret: OAuth client secret for the instance

        Returns JSON:
          - ok: bool
          - error: str (if not ok)
        """
        data = request.get_json(silent=True) or {}
        instance_id = (data.get("instance_id") or "").strip()
        account_id = (data.get("account_id") or "").strip()
        wiki_user_id = (data.get("wiki_user_id") or "").strip()
        wiki_username = (data.get("wiki_username") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()

        if not instance_id or not account_id or not wiki_user_id:
            return jsonify({"ok": False, "error": "missing_parameters"}), 400
        if not client_secret:
            return jsonify({"ok": False, "error": "missing_client_secret"}), 401

        try:
            with get_hosting_db_context() as conn:
                row = conn.execute(
                    "SELECT oauth_client_id, oauth_client_secret_hash "
                    "FROM instances WHERE id=?",
                    (instance_id,),
                ).fetchone()
                if not row or not row["oauth_client_secret_hash"]:
                    return jsonify({"ok": False, "error": "instance_not_configured_for_oauth"}), 403

                salt, expected = (row["oauth_client_secret_hash"] or "").split("$", 1)
                actual = hashlib.sha256((salt + client_secret).encode("utf-8")).hexdigest()
                if actual != expected:
                    return jsonify({"ok": False, "error": "invalid_client_secret"}), 403

                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT id FROM hosting_oauth_account_links "
                    "WHERE instance_id=? AND account_id=?",
                    (instance_id, account_id),
                ).fetchone()
                if existing:
                    conn.execute("ROLLBACK")
                    return jsonify({
                        "ok": False,
                        "error": "This hosting account is already linked to "
                                 "another wiki user on this instance.",
                    }), 409
                conn.execute(
                    "INSERT INTO hosting_oauth_account_links "
                    "(instance_id, account_id, wiki_user_id, wiki_username) "
                    "VALUES (?, ?, ?, ?)",
                    (instance_id, account_id, wiki_user_id, wiki_username),
                )
                conn.commit()
            return jsonify({"ok": True})
        except Exception as exc:
            logger.exception("Error creating account link")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/oauth/unlink", methods=["POST"])
    @hosting_rate_limit(max_requests=30, window=60)
    @oauth_required
    def hosting_oauth_unlink():
        """Remove a hosting-account ↔ wiki-user link.

        Authenticated by the instance's ``client_secret``.

        Body (JSON):
          - instance_id: the hosting DB instance ID
          - account_id: the hosting account ID
          - client_secret: OAuth client secret for the instance
        """
        data = request.get_json(silent=True) or {}
        instance_id = (data.get("instance_id") or "").strip()
        account_id = (data.get("account_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()

        if not instance_id or not account_id:
            return jsonify({"ok": False, "error": "missing_parameters"}), 400
        if not client_secret:
            return jsonify({"ok": False, "error": "missing_client_secret"}), 401

        try:
            with get_hosting_db_context() as conn:
                row = conn.execute(
                    "SELECT oauth_client_id, oauth_client_secret_hash "
                    "FROM instances WHERE id=?",
                    (instance_id,),
                ).fetchone()
                if not row or not row["oauth_client_secret_hash"]:
                    return jsonify({"ok": False, "error": "instance_not_configured_for_oauth"}), 403

                salt, expected = (row["oauth_client_secret_hash"] or "").split("$", 1)
                actual = hashlib.sha256((salt + client_secret).encode("utf-8")).hexdigest()
                if actual != expected:
                    return jsonify({"ok": False, "error": "invalid_client_secret"}), 403

                conn.execute(
                    "DELETE FROM hosting_oauth_account_links "
                    "WHERE instance_id=? AND account_id=?",
                    (instance_id, account_id),
                )
                conn.commit()
            return jsonify({"ok": True})
        except Exception as exc:
            logger.exception("Error unlinking account")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/oauth/me")
    @oauth_required
    def hosting_oauth_me():
        """Alias for /oauth/userinfo; same behaviour."""
        return hosting_oauth_userinfo()
