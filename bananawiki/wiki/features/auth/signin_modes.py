"""How members sign in: the wiki's own accounts, the hosting portal's, or both.

* ``local``: the password form only; every platform sign-in route answers 404
  (links already made stay in the database, unused).
* ``both``: the password form and "sign in with the hosting portal" (the
  default, and the behaviour before modes existed).
* ``platform``: only the portal's sign-in. Password sign-in, the
  administrator sign-in and the sign-up form are closed; new members are
  created by the connect page of the first platform sign-in (under the
  wiki's sign-up policy), and existing members connect their account there
  with its password and two-step code. Administrators keep a way in with
  their password at ``/login/recovery`` (rate-limited and audited).

The administrators choose (``site_settings.signin_mode``, **Admin → Site
settings → Sign-in**); the host may decide instead through
``BW_PLATFORM_SIGNIN_MODE``, which then wins and locks the setting. Without
working platform sign-in configuration the mode is always ``local``.
:func:`current` is the one place that decides.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from ... import auth, settings
from . import platform_oauth

LOCAL, BOTH, PLATFORM = "local", "both", "platform"
MODES = (LOCAL, BOTH, PLATFORM)
DEFAULT = BOTH


def override() -> str:
    """The host's choice (``BW_PLATFORM_SIGNIN_MODE``), or "" when the wiki decides."""
    value = getattr(current_app.config["BW"], "platform_signin_mode", "") or ""
    return value if value in MODES else ""


def stored() -> str:
    """The administrators' choice."""
    value = settings.get("signin_mode")
    return value if value in MODES else DEFAULT


def configured() -> bool:
    return platform_oauth.config() is not None


def current() -> str:
    """The mode in force for this request."""
    if not configured():
        return LOCAL
    return override() or stored()


def platform_allowed() -> bool:
    """Whether the hosting portal's sign-in (and linking) is offered."""
    return current() != LOCAL


def password_allowed() -> bool:
    """Whether the wiki's own password sign-in (and its sign-up form) is offered."""
    return current() != PLATFORM


def platform_refusal(user: dict[str, Any] | None) -> str | None:
    """Why *user* may not switch the wiki to ``platform`` (a translation key), or None.

    An administrator whose own account is not linked to a hosting account
    would lock themselves out with the switch.
    """
    if not configured():
        return "auth.signin_mode.error.not_configured"
    if user is None or auth.on_behalf() or platform_oauth.link_of(user["id"]) is None:
        return "auth.signin_mode.error.link_yours_first"
    return None
