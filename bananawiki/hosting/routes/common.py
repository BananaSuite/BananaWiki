"""Helpers shared by the route modules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from flask import abort, flash, redirect, request, url_for

from ...core.timeutil import to_sql, utcnow
from ...core.web import is_safe_redirect
from .. import auth, collaborators, instances
from ..errors import ServiceError
from ..i18n import t

MAX_SECONDS = 10 * 365 * 86400


def account() -> dict[str, Any]:
    current = auth.current_account()
    if current is None:
        abort(401)
    return current


def flash_error(error: ServiceError) -> None:
    flash(t(error.key, **error.values), "error")


def back(default: str) -> Any:
    """Redirect to a same-site ``next`` form field, else *default*."""
    target = request.form.get("next") or request.args.get("next")
    return redirect(target if is_safe_redirect(target) else default)


def instance_for(instance_id: str, permission: str = "view") -> dict[str, Any]:
    """The wiki if the current account may use *permission* on it, else 404.

    Owners and administrators hold every permission; collaborators hold what
    they were granted. Anything else, including terminated wikis the viewer
    may not recover, answers 404 so ids cannot be probed.
    """
    viewer = account()
    inst = instances.get(instance_id)
    if inst is None or not collaborators.can(inst, viewer, permission):
        abort(404)
    if inst["status"] == "terminated" and not viewer["is_admin"]:
        from .. import settings

        if inst["account_id"] != viewer["id"] or not settings.flag("allow_owner_download_expired"):
            abort(404)
    return inst


def owner_locked(inst: dict[str, Any]) -> bool:
    """Non-administrators cannot act on a suspended wiki."""
    return inst["status"] == "suspended" and not account()["is_admin"]


def detail_url(instance_id: str) -> str:
    return url_for("dashboard.instance_detail", instance_id=instance_id)


def admin_back(instance_id: str) -> Any:
    return back(url_for("admin.instance", instance_id=instance_id))


def int_field(name: str, default: int = 0, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = (request.form.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as error:
        raise ServiceError("hosting.form.invalid_number") from error
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ServiceError("hosting.form.invalid_number")
    return value


def seconds_from_form(prefix: str) -> int:
    """Days/hours/minutes/seconds fields named ``<prefix>_days`` etc."""
    total = (int_field(f"{prefix}_days", minimum=0) * 86400 + int_field(f"{prefix}_hours", minimum=0) * 3600
             + int_field(f"{prefix}_minutes", minimum=0) * 60 + int_field(f"{prefix}_seconds", minimum=0))
    return min(total, MAX_SECONDS)


def utc_from_input(value: str) -> str | None:
    """A ``datetime-local`` value (interpreted as UTC) in storage form."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise ServiceError("hosting.form.invalid_datetime") from error
    return to_sql(moment)


def suspension_from_form() -> tuple[str | None, str]:
    """``(until, label)`` from the shared suspension form (1.4 field names)."""
    choice = request.form.get("suspend_duration", "permanent")
    if choice == "custom_datetime":
        until = utc_from_input(request.form.get("suspend_custom_datetime", "").strip())
        if not until or until <= to_sql(utcnow()):  # type: ignore[operator]
            raise ServiceError("hosting.form.invalid_datetime")
        return until, until
    if choice == "custom_relative":
        seconds = seconds_from_form("suspend_rel")
        if seconds <= 0:
            return None, "permanent"
        return to_sql(utcnow() + timedelta(seconds=seconds)), f"{seconds}s"
    if choice != "permanent":
        try:
            hours = int(choice)
        except ValueError as error:
            raise ServiceError("hosting.form.invalid_number") from error
        if hours > 0:
            return to_sql(utcnow() + timedelta(hours=min(hours, MAX_SECONDS // 3600))), f"{hours}h"
    return None, "permanent"
