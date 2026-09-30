"""Self-service account creation: sign-up policy and registration.

Sign-up is possible with a valid invite code, or without one while open
sign-up is active (``open_signup`` and, optionally, until
``open_signup_until``). With ``approval_required`` the new account starts
``pending`` and the request pipeline keeps it on the account-status page
until an administrator approves it. The same rules apply to accounts created
through the hosting portal's single sign-on.
"""

from __future__ import annotations

from typing import Any

from ... import accounts, settings
from ...db import db
from ...registry import emit
from . import invites


def signup_available() -> bool:
    """Whether a visitor may try to create an account right now."""
    return settings.setup_done() and not settings.maintenance_active()


def invite_required() -> bool:
    return not settings.open_signup_active()


def register(
    username: str,
    password: str | None,
    *,
    invite_code: str | None = None,
    password_hash: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an account under the current sign-up policy.

    The invite is checked and consumed in the same transaction that creates
    the account, so a single-use code can never create two accounts.
    Raises :class:`accounts.AccountError` with a translation key on refusal.
    """
    if not signup_available():
        raise accounts.AccountError("auth.signup.error.closed")
    accounts.validate_username(username)
    if password_hash is None:
        # Hash before taking the write lock: it is the slow part.
        password_hash = accounts.hash_password(accounts.validate_password(password))
    needs_invite = invite_required()
    if needs_invite and not invites.normalize(invite_code):
        raise accounts.AccountError("auth.signup.error.invite_required")
    status = "pending" if settings.approval_required() else "approved"
    with db.transaction():
        invite = invites.find_valid(invite_code) if needs_invite else None
        if needs_invite and invite is None:
            raise accounts.AccountError("auth.signup.error.invite_invalid")
        role, custom_role_id = invites.assigned_role(invite) if invite else ("user", None)
        user = accounts.create(
            username, None, role=role, custom_role_id=custom_role_id, approval_status=status,
            invite_code=invite["code"] if invite else None, password_hash=password_hash, extra=extra,
            emit_event=False,
        )
        if invite is not None and not invites.consume(invite, user["id"]):
            raise accounts.AccountError("auth.signup.error.invite_invalid")
    emit("user.created", user=user)
    return user
