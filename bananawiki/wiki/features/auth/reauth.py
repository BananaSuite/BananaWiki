"""Confirming a sensitive change with the hosting portal instead of the password.

Wherever the wiki asks for the account's password before a change (the
password itself, two-step verification, the email address, the username,
deleting the account, merges, the owner role, plugins, site export and
import), a linked account may choose **Confirm with BananaWiki Hosting**:
the portal is asked for a fresh sign-in (``prompt=login``, ``max_age=0``),
with a state bound to this session, the account and the page to return to.
The answer counts only for the portal account linked here, only when it
carries an ``auth_time`` from after the confirmation started (60 seconds of
clock difference allowed; a portal that sends none never confirms) and
never while a hosting administrator acts as the account. It then stands for
the password for :data:`~bananawiki.wiki.auth.REVERIFY_SECONDS` in this
session (``user_sessions.reverified_at``), checked by
:func:`bananawiki.wiki.features.users.service.password_ok`.

Accounts without a password of their own (``users.password_set`` 0: made
through the portal's sign-in) see only this option. While a hosting
administrator acts as the account, these changes are refused.
"""

from __future__ import annotations

import math
from typing import Any

from flask import g

from ....core.timeutil import parse, utcnow
from ....core.web import allow_form_action
from ... import auth
from . import platform_oauth as oauth
from . import signin_modes
from .blueprint import bp

CLOCK_SKEW_SECONDS = 60


def has_password(user: dict[str, Any] | None) -> bool:
    return bool(user) and bool(user.get("password_set", 1))  # type: ignore[union-attr]


def hosting_available(user: dict[str, Any] | None) -> bool:
    """Whether *user* may confirm with the hosting portal here: platform sign-in is offered, the account is
    linked, and nobody acts on its behalf."""
    return (bool(user) and signin_modes.platform_allowed() and not auth.on_behalf()
            and oauth.link_of(user["id"]) is not None)  # type: ignore[index]


def state(user: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a page asking for the password offers (cached per request): ``password`` (the account has one),
    ``hosting`` (it may confirm with the portal), ``verified`` (it just did: no password needed),
    ``minutes`` (how long that lasts) and ``refused`` (someone acts on its behalf: the change is refused)."""
    cached = g.get("_reauth_state")
    if cached is not None:
        return cached
    user = user or auth.current_user()
    verified = auth.reverified_recently(user)
    minutes = 0
    if verified:
        row = g.get("auth_session") or {}
        confirmed = parse(row.get("reverified_at"))
        left = auth.REVERIFY_SECONDS - (utcnow() - confirmed).total_seconds() if confirmed else 0
        minutes = max(1, math.ceil(left / 60))
    hosting = hosting_available(user)
    if hosting and not verified:
        # The confirmation form is answered with a redirect to the portal.
        allow_form_action(oauth.config()["authorize_url"])  # type: ignore[index]
    result = {"password": has_password(user), "hosting": hosting, "verified": verified, "minutes": minutes,
              "refused": auth.acting() is not None}
    g._reauth_state = result
    return result


def accepted(entry: dict[str, Any], identity: oauth.Identity, user: dict[str, Any]) -> str | None:
    """Why the portal's answer to a confirmation is refused (a translation key), or None when it confirms."""
    link = oauth.link_of(user["id"])
    if identity.act is not None or auth.on_behalf():
        return "auth.acting.error.refused"
    if link is None or link["account_id"] != identity.sub:
        return "auth.reverify.error.other_account"
    try:
        started = float(entry.get("started", 0))
    except (TypeError, ValueError):
        return "auth.oauth.error.expired"
    if identity.auth_time is None or identity.auth_time < started - CLOCK_SKEW_SECONDS:
        return "auth.reverify.error.not_fresh"
    return None


@bp.app_template_global("reauth_state")
def reauth_state() -> dict[str, Any]:
    """For the ``current_password`` macro (``users/_macros.html``)."""
    return state()
