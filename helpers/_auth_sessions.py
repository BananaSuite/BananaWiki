"""Request helpers for standalone BananaWiki login sessions."""

from flask import request, session
import uuid

import db
from ._session_metadata import (
    normalize_session_ip,
    normalize_session_user_agent,
)


SESSION_TOKEN_KEY = "auth_session_token"


def establish_user_session(
    user_id,
    *,
    remember_me=False,
    auth_method="password",
    revoke_existing=None,
):
    """Persist a login and attach its opaque bearer token to the cookie."""
    if revoke_existing is None:
        settings = db.get_site_settings() or {}
        revoke_existing = bool(settings.get("session_limit_enabled"))
        if revoke_existing:
            legacy_token = uuid.uuid4().hex
            db.update_user(user_id, session_token=legacy_token)
            session["session_token"] = legacy_token
        else:
            user = db.get_user_by_id(user_id)
            if user and user.get("session_token") is not None:
                db.update_user(user_id, session_token=None)
            session.pop("session_token", None)
    row_id, raw_token = db.create_user_session(
        user_id,
        ip_address=normalize_session_ip(request.remote_addr),
        user_agent=normalize_session_user_agent(request.headers.get("User-Agent")),
        remember_me=remember_me,
        auth_method=auth_method,
        revoke_existing=bool(revoke_existing),
    )
    session[SESSION_TOKEN_KEY] = raw_token
    return row_id


def current_user_session_id():
    """Return the current active persistent-session ID, if present."""
    return db.get_user_session_id(session.get(SESSION_TOKEN_KEY))
