"""Administrator pages: monitoring conversations and groups, and the messaging settings."""

from __future__ import annotations

import logging
import math
from typing import Any

from flask import abort, current_app, render_template, request, url_for
from jinja2 import TemplateNotFound

from ... import accounts, auth, settings
from ...i18n import t
from . import dms, groups, retention
from .policy import DEFAULT_ATTACHMENT_MB, MAX_LENGTH_CEILING, MB
from .routes_groups import display_name
from .store import DM, GROUP
from .web import bp, conversation, page_number, refuse, succeed

log = logging.getLogger("bananawiki.chat")
PER_PAGE = 50

BOOLEAN_SETTINGS = (
    "chat_dm_enabled", "chat_group_enabled", "chat_allow_dm_creation", "chat_allow_group_creation",
    "chat_attachments_enabled", "chat_cleanup_enabled", "chat_dm_auto_clear_messages",
    "chat_dm_auto_clear_attachments", "chat_group_auto_clear_messages", "chat_group_auto_clear_attachments",
    "profile_group_badges_enabled",
)
RETENTION_DAYS = (
    ("chat_dm_message_retention_days", "chat_dm_auto_clear_messages"),
    ("chat_dm_attachment_retention_days", "chat_dm_auto_clear_attachments"),
    ("chat_group_message_retention_days", "chat_group_auto_clear_messages"),
    ("chat_group_attachment_retention_days", "chat_group_auto_clear_attachments"),
)


def admin_layout() -> str:
    """The admin area layout when the admin feature provides one."""
    try:
        current_app.jinja_env.get_template("admin/_layout.html")
    except TemplateNotFound:
        return "chat/_admin_fallback.html"
    return "admin/_layout.html"


def _render(template: str, **context: Any) -> str:
    return render_template(template, layout=admin_layout(), **context)


def _pages(total: int) -> int:
    return max(1, math.ceil(total / PER_PAGE))


@bp.route("/admin/chats")
@auth.admin_required
def admin_chats():
    username = (request.args.get("user") or "").strip()
    filter_user = accounts.by_id(request.args.get("user_id")) if request.args.get("user_id") else None
    if username:
        filter_user = accounts.by_username(username)
    page = page_number()
    rows, total = ([], 0) if (username or request.args.get("user_id")) and filter_user is None else \
        dms.admin_page(user_id=filter_user["id"] if filter_user else None, page=page, per_page=PER_PAGE)
    return _render("chat/admin_chats.html", chats=rows, page=page, pages=_pages(total), filter_user=filter_user,
                   username=username or (filter_user["username"] if filter_user else ""))


@bp.route("/admin/chats/<int:chat_id>")
@auth.admin_required
def admin_chat_view(chat_id: int):
    chat = dms.get(chat_id)
    if chat is None:
        abort(404)
    log.info("Administrator %s read chat %s", auth.current_user()["id"], chat_id)
    return _render("chat/admin_chat_view.html", chat=chat, first=accounts.by_id(chat["user1_id"]),
                   second=accounts.by_id(chat["user2_id"]), conversation=conversation(DM, chat_id, with_ip=True))


@bp.route("/admin/groups")
@auth.admin_required
def admin_groups():
    page = page_number()
    rows, total = groups.admin_page(page=page, per_page=PER_PAGE)
    return _render("chat/admin_groups.html", groups=rows, page=page, pages=_pages(total), display_name=display_name)


@bp.route("/admin/groups/<int:group_id>")
@auth.admin_required
def admin_group_view(group_id: int):
    group = groups.get(group_id)
    if group is None:
        abort(404)
    return _render("chat/admin_group_view.html", group=group, name=display_name(group),
                   members=groups.members(group_id), banned=groups.banned_members(group_id),
                   conversation=conversation(GROUP, group_id, with_ip=True))


@bp.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
@auth.admin_required
def admin_group_delete(group_id: int):
    group = groups.get(group_id)
    if group is None:
        abort(404)
    if group["is_global"]:
        return refuse("chat.groups.error.global_undeletable", url_for("chat.admin_groups"), 409)
    groups.delete(group_id)
    log.info("Group %s deleted by administrator %s", group_id, auth.current_user()["id"])
    return succeed(url_for("chat.admin_groups"), "chat.groups.deleted", values={"name": group["name"]})


def _int_field(name: str, minimum: int, maximum: int) -> int:
    raw = (request.form.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(name) from error
    if not minimum <= value <= maximum:
        raise ValueError(name)
    return value


def _settings_from_form() -> dict[str, Any]:
    ceiling_mb = max(1, current_app.config["BW"].max_attachment_size // MB)
    values: dict[str, Any] = {name: 1 if request.form.get(name) else 0 for name in BOOLEAN_SETTINGS}
    values["chat_max_message_length"] = _int_field("chat_max_message_length", 1, MAX_LENGTH_CEILING)
    values["chat_max_attachment_size_mb"] = _int_field("chat_max_attachment_size_mb", 1, ceiling_mb)
    values["chat_attachments_per_day_limit"] = _int_field("chat_attachments_per_day_limit", 1, 100_000)
    values["chat_cleanup_frequency_days"] = _int_field("chat_cleanup_frequency_days", 1, 365)
    values["chat_cleanup_hour"] = _int_field("chat_cleanup_hour", 0, 23)
    for days, switch in RETENTION_DAYS:
        values[days] = _int_field(days, 1 if values[switch] else 0, 36_500)
    values["chat_cleanup_split_configured"] = 1
    return values


@bp.route("/admin/chats/settings", methods=["GET", "POST"])
@auth.admin_required
def admin_settings():
    error_field = None
    if request.method == "POST":
        try:
            values = _settings_from_form()
        except ValueError as error:
            error_field = str(error)
        else:
            settings.update(values)
            return succeed(url_for("chat.admin_settings"), "common.saved")
    current = dict(settings.load())
    current.update(retention.policy_columns())
    return _render(
        "chat/admin_settings.html", values=current, error_field=error_field,
        error=t("chat.settings.error.invalid", field=t(f"chat.settings.{error_field}")) if error_field else None,
        next_run=retention.next_run(), last_run=settings.get("last_chat_cleanup_at"),
        ceiling_mb=max(1, current_app.config["BW"].max_attachment_size // MB),
        default_mb=DEFAULT_ATTACHMENT_MB,
    ), 400 if error_field else 200

