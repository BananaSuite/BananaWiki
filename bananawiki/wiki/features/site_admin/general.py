"""General site settings: name, time zone, access modes, sign-in, uploads and exports.

Settings that belong to one feature (chat, kanban, page governance, text to
speech, …) live on that feature's own admin page; this page holds the
site-wide ones that 1.4 kept on ``/global-settings``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from zoneinfo import available_timezones

from flask import current_app, redirect, render_template, request, url_for

from ... import auth, registry, settings
from ...i18n import t
from ..audit import record
from .blueprint import bp
from .fields import Field, parse_form

ROLES = ("user", "editor", "admin")


def _cfg():
    return current_app.config["BW"]


def _public_mode_locked() -> str | None:
    return "site_admin.locked.public_mode" if _cfg().forbid_public_mode else None


def _page_builder_locked() -> str | None:
    return "site_admin.locked.page_builder" if _cfg().forbid_page_builder else None


def _platform_owned() -> str | None:
    return "site_admin.locked.platform" if _cfg().managed_hosting else None


@lru_cache(maxsize=1)
def time_zones() -> tuple[str, ...]:
    return tuple(sorted(available_timezones() | {"UTC"}))


def _profiles_enabled() -> bool:
    return registry.is_enabled("user_profiles")


SECTIONS: list[tuple[str, list[Field]]] = [
    ("general", [
        Field("site_name", "text", max_length=100, empty="BananaWiki"),
        Field("timezone", "choice", choices=time_zones),
    ]),
    ("access", [
        Field("public_mode", "flag", locked=_public_mode_locked),
        Field("public_mode_until", "until", depends_on="public_mode", locked=_public_mode_locked),
        Field("public_mode_show_message", "flag", locked=_public_mode_locked),
        Field("public_mode_message", "textarea", max_length=1000, empty="", locked=_public_mode_locked),
        Field("open_signup", "flag"),
        Field("open_signup_until", "until", depends_on="open_signup"),
        Field("approval_required", "flag"),
        Field("approval_pending_timeout_hours", "number", minimum=0, maximum=8760),
        Field("approval_denied_timeout_hours", "number", minimum=1, maximum=8760),
        Field("maintenance_mode", "flag"),
        Field("maintenance_message", "textarea", max_length=1000, empty=""),
    ]),
    ("security", [
        Field("session_limit_enabled", "flag"),
        Field("bot_protection_enabled", "flag"),
        Field("suspended_account_deletion_enabled", "flag"),
    ]),
    ("signin", [
        Field("login_app_selector", "flag"),
        Field("new_user_intro_enabled", "flag"),
        Field("onboarding_replay_disabled", "flag"),
        Field("intro_role_switching_enabled", "flag"),
        Field("intro_role_switching_roles", "roles", options=ROLES),
    ]),
    ("uploads", [
        Field("upload_mode", "choice", options=("allow_all", "whitelist", "blacklist")),
        Field("upload_whitelist", "extensions", max_length=2000),
        Field("upload_blacklist", "extensions", max_length=2000),
        Field("upload_max_size_mb", "number", minimum=1, maximum=2048, locked=_platform_owned),
        Field("upload_quota_per_day_count", "number", minimum=0, maximum=100_000),
        Field("upload_quota_per_day_bytes", "megabytes", minimum=0, maximum=100_000),
    ]),
    ("content", [
        Field("draft_expiration_hours", "number", minimum=0, maximum=8760),
        Field("pdf_export_enabled", "flag"),
        Field("markdown_export_enabled", "flag"),
        Field("page_builder_enabled", "flag", locked=_page_builder_locked),
        Field("profile_contribution_chart_enabled", "flag", visible=_profiles_enabled),
    ]),
]


def all_fields() -> list[Field]:
    return [item for _section, fields in SECTIONS for item in fields]


def _current_values() -> dict[str, Any]:
    """Stored values as the form shows them; expired time limits show as off."""
    stored = settings.load()
    values = {item.name: item.display(stored.get(item.name)) for item in all_fields()}
    active = {"public_mode": settings.public_mode_active(), "open_signup": settings.open_signup_active()}
    for flag, until in (("public_mode", "public_mode_until"), ("open_signup", "open_signup_until")):
        if values.get(flag) and stored.get(until) and not active[flag]:
            values[flag], values[until] = 0, None
    return values


def _submitted_values(values: dict[str, Any]) -> dict[str, Any]:
    shown = dict(_current_values())
    for item in all_fields():
        if item.name in values:
            shown[item.name] = item.display(values[item.name])
        elif item.kind != "flag" and item.name in request.form and not item.lock_reason():
            shown[item.name] = request.form.get(item.name)
        if item.kind == "roles" and not item.lock_reason():
            shown[item.name] = request.form.getlist(item.name)
    return shown


def _platform_blacklist() -> list[str]:
    stored = str(settings.get("platform_upload_blacklist") or "")
    items = list(_cfg().platform_upload_blacklist) + [x.strip().lower() for x in stored.split(",") if x.strip()]
    return sorted(set(items))


def _render(values: dict[str, Any], errors: dict[str, Any], status: int = 200):
    return render_template(
        "site_admin/general.html",
        sections=SECTIONS, values=values, errors=errors, platform_blacklist=_platform_blacklist(),
        role_options=ROLES,
    ), status


@bp.route("/admin/settings", methods=["GET", "POST"])
@bp.route("/global-settings", methods=["GET", "POST"], endpoint="general_legacy")
@auth.admin_required
def general():
    if request.method == "GET":
        if request.endpoint == "site_admin.general_legacy":
            return redirect(url_for("site_admin.general"))
        return _render(_current_values(), {})
    values, errors = parse_form(all_fields(), request.form)
    if errors:
        auth.flash_t("site_admin.error.form", "error")
        messages = {name: t(error.key, **error.values) for name, error in errors.items()}
        return _render(_submitted_values(values), messages, 400)
    previous = settings.load()
    changed = sorted(name for name, value in values.items() if previous.get(name) != value)
    settings.update(values)
    if changed:
        record("settings.updated", details={"changed": ", ".join(changed)})
    auth.flash_t("site_admin.settings.saved", "success")
    return redirect(url_for("site_admin.general"))
