"""Group chats: /groups…

Every view, moderation actions included, first checks that the user may use
group chats at all (``chat.group`` permission and ``chat_group_enabled``);
1.4 skipped that check on the moderation routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from flask import abort, jsonify, redirect, render_template, request, url_for

from ....core.ratelimit import SqlLimiter
from ....core.timeutil import is_future, sql_in
from ....core.web import client_ip, safe_next
from ... import accounts, auth, storage
from ...db import db
from ...i18n import t
from . import exports, groups, policy, retention, store
from .store import GROUP
from .web import (
    ACTIONS_PER_MINUTE,
    EXPORTS_PER_MINUTE,
    POLL_PER_MINUTE,
    SEND_PER_MINUTE,
    Refused,
    bp,
    conversation,
    halt,
    messages_payload,
    read_message_input,
    refuse,
    refused,
    succeed,
    throttle,
    too_many,
)

log = logging.getLogger("bananawiki.chat")

JOIN_WINDOW = 15 * 60
JOIN_MAX_PER_USER = 10
JOIN_MAX_PER_IP = 30
TIMEOUT_CHOICES = (5, 30, 60, 1440, 10080)
TIMEOUT_MAX_MINUTES = 525_600


@dataclass(frozen=True)
class Actor:
    """The current user in relation to one group."""

    user: dict[str, Any]
    group: dict[str, Any]
    membership: dict[str, Any] | None
    admin: bool

    @property
    def role(self) -> str | None:
        return self.membership["role"] if self.membership else None

    @property
    def is_member(self) -> bool:
        return self.membership is not None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def is_moderator(self) -> bool:
        return self.role in groups.MODERATOR_ROLES

    @property
    def can_moderate(self) -> bool:
        """Timeouts, removals, bans: site administrators only in the global room."""
        if self.group["is_global"]:
            return self.admin
        return self.admin or self.is_moderator

    @property
    def can_manage(self) -> bool:
        """Owner-level actions: roles, invite code, export, deletion."""
        if self.group["is_global"]:
            return self.admin
        return self.admin or self.is_owner

    @property
    def can_delete_any(self) -> bool:
        return self.can_moderate

    def outranks(self, target: dict[str, Any]) -> bool:
        """Moderators may not act on the owner or other moderators; nobody acts on the owner."""
        if target["role"] == "owner":
            return False
        return self.admin or self.is_owner or target["role"] == "member"


def _require_groups() -> None:
    if not policy.group_allowed():
        halt(auth.deny(403, "chat.error.groups_unavailable"))


def _actor(group_id: int) -> Actor:
    _require_groups()
    group = groups.get(group_id)
    if group is None:
        halt(auth.deny(404, "chat.error.not_found"))
    user = auth.current_user()
    return Actor(user, group, groups.member(group_id, user["id"]), auth.is_admin(user))


def _authority(group_id: int, *, manage: bool = False) -> Actor:
    """Current authority, re-read while the caller holds its writer transaction."""
    actor = _actor(group_id)
    if not (actor.can_manage if manage else actor.can_moderate):
        key = "chat.groups.error.owner_only" if manage else "chat.groups.error.not_allowed"
        halt(refuse(key, _back(group_id), 403))
    return actor


def _reader(group_id: int) -> Actor:
    """An actor who may read the group: a member or a site administrator."""
    actor = _actor(group_id)
    if actor.is_member or actor.admin:
        return actor
    row = groups.membership(group_id, actor.user["id"])
    key = "chat.groups.error.banned" if row and row["banned"] else "chat.groups.error.not_member"
    halt(refuse(key, url_for("chat.group_list"), 403))


def _back(group_id: int) -> str:
    return url_for("chat.group_view", group_id=group_id)


def _target(actor: Actor) -> dict[str, Any] | None:
    """The (non-banned) member named by the ``user_id`` form field, with their username."""
    user_id = request.form.get("user_id") or ""
    row = groups.member(actor.group["id"], user_id) if user_id else None
    if row is None:
        return None
    target = dict(row)
    account = accounts.by_id(user_id)
    target["username"] = account["username"] if account else t("common.unknown_user")
    return target


def display_name(group: dict[str, Any]) -> str:
    return t("chat.groups.global_name") if group["is_global"] else group["name"]


def _throttled() -> bool:
    return not throttle("action", ACTIONS_PER_MINUTE)


# ── Lists, creating and joining ─────────────────────────────────────────────


@bp.route("/groups")
def group_list():
    _require_groups()
    user = auth.current_user()
    return render_template("chat/group_list.html", groups=groups.list_for(user["id"]),
                           can_create=policy.can_create_group(user), display_name=display_name)


@bp.route("/groups/new", methods=["GET", "POST"])
def group_new():
    _require_groups()
    if not policy.can_create_group():
        return refuse("chat.groups.error.creation_disabled", url_for("chat.group_list"), 403)
    if request.method == "GET":
        return render_template("chat/group_new.html", form={})
    if _throttled():
        return too_many()
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    error = None
    if not name:
        error = t("chat.groups.error.name_required")
    elif len(name) > groups.NAME_MAX:
        error = t("chat.groups.error.name_too_long", limit=groups.NAME_MAX)
    elif len(description) > groups.DESCRIPTION_MAX:
        error = t("chat.groups.error.description_too_long", limit=groups.DESCRIPTION_MAX)
    if error:
        return render_template("chat/group_new.html", form=request.form, error=error), 400
    group = groups.create(name, description, auth.current_user())
    return succeed(_back(group["id"]), "chat.groups.created", group_id=group["id"])


@bp.route("/groups/join", methods=["GET", "POST"])
def group_join():
    _require_groups()
    if request.method == "GET":
        return render_template("chat/group_join.html")
    user = auth.current_user()
    limiter = SqlLimiter(db.session)
    if not (limiter.hit(user["id"], "group_join", JOIN_MAX_PER_USER, JOIN_WINDOW)
            and limiter.hit(client_ip(), "group_join_ip", JOIN_MAX_PER_IP, JOIN_WINDOW)):
        return refuse("chat.groups.error.join_rate_limited", url_for("chat.group_join"), 429)
    code = (request.form.get("invite_code") or "").strip()
    if not code:
        return refuse("chat.groups.error.code_required", url_for("chat.group_join"))
    group = groups.by_invite(code)
    if group is None:
        return refuse("chat.groups.error.bad_code", url_for("chat.group_join"), 404)
    return _join(group, user)


def _join(group: dict[str, Any], user: dict[str, Any]):
    row = groups.membership(group["id"], user["id"])
    if row and row["banned"]:
        return refuse("chat.groups.error.banned", url_for("chat.group_list"), 403)
    if row is None:
        with db.transaction():
            if groups.add_member(group["id"], user["id"]):
                groups.system(group["id"], "chat.system.joined", user=user["username"])
        return succeed(_back(group["id"]), "chat.groups.joined", values={"name": display_name(group)},
                       group_id=group["id"])
    return succeed(_back(group["id"]), group_id=group["id"])


@bp.route("/groups/global", methods=["GET", "POST"])
def group_global():
    """Open the global room; joining it is a POST (a link or an image must not sign anyone up)."""
    _require_groups()
    user = auth.current_user()
    if request.method == "POST":
        return _join(groups.global_group(), user)
    group = groups.find_global()
    if group is not None:
        member = groups.membership(group["id"], user["id"])
        if (member and not member["banned"]) or auth.is_admin(user):
            return redirect(url_for("chat.group_view", group_id=group["id"]))
    return render_template("chat/group_global.html")


# ── The room ─────────────────────────────────────────────────────────────────


@bp.route("/groups/<int:group_id>")
def group_view(group_id: int):
    actor = _reader(group_id)
    if actor.is_member:
        groups.mark_read(group_id, actor.user["id"])
    group = actor.group
    return render_template(
        "chat/group_view.html", group=group, name=display_name(group), actor=actor,
        conversation=conversation(GROUP, group_id, can_delete_any=actor.can_delete_any),
        members=groups.members(group_id),
        banned=groups.banned_members(group_id) if actor.can_moderate else [],
        timed_out=groups.is_timed_out(actor.membership), indefinite=groups.is_indefinite,
        is_future=is_future, can_upload=policy.can_upload(actor.user),
        can_send=actor.is_member and (group["is_active"] or not group["is_global"]),
        max_length=policy.max_message_length(), max_attachment_bytes=policy.max_attachment_bytes(),
        per_day=policy.attachments_per_day(), retention=retention.active_rules("group"),
        counts=store.count(GROUP, group_id), timeout_choices=TIMEOUT_CHOICES,
    )


@bp.route("/groups/<int:group_id>/messages")
def group_messages(group_id: int):
    actor = _reader(group_id)
    if not throttle("poll", POLL_PER_MINUTE):
        return too_many()
    return jsonify(messages_payload(GROUP, group_id, can_delete_any=actor.can_delete_any))


@bp.route("/groups/<int:group_id>/read", methods=["POST"])
def group_read(group_id: int):
    actor = _reader(group_id)
    groups.mark_read(group_id, actor.user["id"])
    return jsonify({"ok": True})


@bp.route("/groups/<int:group_id>/send", methods=["POST"])
def group_send(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    row = groups.membership(group_id, actor.user["id"])
    if row and row["banned"]:
        return refuse("chat.groups.error.banned", url_for("chat.group_list"), 403)
    if not actor.is_member:
        return refuse("chat.groups.error.not_member", url_for("chat.group_list"), 403)
    if actor.group["is_global"] and not actor.group["is_active"]:
        return refuse("chat.groups.error.global_paused", back, 403)
    if groups.is_timed_out(actor.membership):
        return refuse("chat.groups.error.timed_out", back, 403)
    if not throttle("send", SEND_PER_MINUTE):
        return too_many()
    try:
        content, stored = read_message_input(actor.user)
    except Refused as error:
        return refused(error, back)
    try:
        message_id = groups.send(actor.group, actor.user["id"], content, ip_address=client_ip(), stored=stored)
    except groups.SendRefused as error:
        if stored is not None:
            storage.delete(store.FOLDER, stored.filename)
        return refused(Refused(error.key, error.status), back)
    except store.AttachmentLimitExceeded as error:
        if stored is not None:
            storage.delete(store.FOLDER, stored.filename)
        return refused(Refused("chat.error.daily_limit", 429, limit=error.limit), back)
    except BaseException:
        if stored is not None:
            storage.delete(store.FOLDER, stored.filename)
        raise
    return succeed(back, message_id=message_id)


@bp.route("/groups/<int:group_id>/delete_message", methods=["POST"])
def group_delete_message(group_id: int):
    actor = _reader(group_id)
    back = _back(group_id)
    if _throttled():
        return too_many()
    message = store.message(GROUP, request.form.get("message_id", type=int) or 0)
    if message is None or message["parent_id"] != group_id:
        return refuse("chat.error.message_not_found", back, 404)
    if message["is_system"]:
        return refuse("chat.groups.error.system_message", back, 403)
    own = message["sender_id"] == actor.user["id"]
    if not own and not actor.can_delete_any:
        return refuse("chat.error.not_your_message", back, 403)

    def authorize_deletion():
        current_actor = _reader(group_id)
        current = store.message(GROUP, message["id"])
        if current is None or current["parent_id"] != group_id:
            halt(refuse("chat.error.message_not_found", back, 404))
        if current["is_system"]:
            halt(refuse("chat.groups.error.system_message", back, 403))
        if current["sender_id"] != current_actor.user["id"] and not current_actor.can_delete_any:
            halt(refuse("chat.error.not_your_message", back, 403))

    if not message["is_deleted"]:
        store.wipe(GROUP, message["id"], authorize=authorize_deletion)
        if not own:
            groups.system(group_id, "chat.system.message_deleted", actor=actor.user["username"])
    return succeed(back, "chat.message.deleted_flash", message_id=message["id"])


@bp.route("/groups/attachments/<int:attachment_id>/download")
def group_attachment(attachment_id: int):
    """Members (not banned) and site administrators may download attachments."""
    user = auth.current_user()
    item = store.attachment(GROUP, attachment_id)
    admin = auth.is_admin(user)
    if item is None or item["is_deleted"] or not (admin or policy.group_allowed(user)):
        abort(404)
    if not admin and groups.member(item["parent_id"], user["id"]) is None:
        abort(404)
    return storage.send(store.FOLDER, item["filename"], download_name=item["original_name"],
                        blob_id=item["blob_id"], max_age=0)


@bp.route("/groups/<int:group_id>/export")
def group_export(group_id: int):
    actor = _reader(group_id)
    if not actor.can_manage:
        return refuse("chat.groups.error.owner_only", _back(group_id), 403)
    if not throttle("export", EXPORTS_PER_MINUTE):
        return too_many()
    name = display_name(actor.group)
    return exports.export(GROUP, group_id, title=f"group_{name}", header=[t("chat.export.group_title", name=name)],
                          include_ip=actor.admin)


# ── Membership ──────────────────────────────────────────────────────────────


@bp.route("/groups/<int:group_id>/members/add", methods=["POST"])
def group_add_member(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.can_moderate:
        return refuse("chat.groups.error.not_allowed", back, 403)
    if _throttled():
        return too_many()
    username = (request.form.get("username") or "").strip()
    with db.transaction():
        actor = _authority(group_id)
        target = accounts.by_username(username) if username else None
        if target is None or not policy.can_join_groups(target):
            return refuse("chat.error.user_unavailable", back, 404)
        row = groups.membership(group_id, target["id"])
        if row and row["banned"]:
            return refuse("chat.groups.error.target_banned", back, 409)
        if row:
            return refuse("chat.groups.error.already_member", back, 409)
        groups.add_member(group_id, target["id"])
        groups.system(group_id, "chat.system.added", user=target["username"], actor=actor.user["username"])
    return succeed(back, "chat.groups.member_added", values={"user": target["username"]})


@bp.route("/groups/<int:group_id>/leave", methods=["POST"])
def group_leave(group_id: int):
    with db.transaction():
        actor = _actor(group_id)
        if not actor.is_member:
            return refuse("chat.groups.error.not_member", url_for("chat.group_list"), 403)
        if actor.is_owner and not actor.group["is_global"]:
            return refuse("chat.groups.error.owner_must_transfer", _back(group_id), 409)
        groups.remove_member(group_id, actor.user["id"])
        groups.system(group_id, "chat.system.left", user=actor.user["username"])
    return succeed(url_for("chat.group_list"), "chat.groups.left")


@bp.route("/groups/<int:group_id>/kick", methods=["POST"])
def group_kick(group_id: int):
    """Remove a member, or ban them with ``permanent=1``."""
    actor = _actor(group_id)
    back = _back(group_id)
    if _throttled():
        return too_many()
    ban = request.form.get("permanent") == "1"
    with db.transaction():
        actor = _actor(group_id)
        if not actor.can_moderate:
            return refuse("chat.groups.error.not_allowed", back, 403)
        target = _target(actor)
        if target is None:
            return refuse("chat.groups.error.target_not_member", back, 404)
        if target["user_id"] == actor.user["id"]:
            return refuse("chat.groups.error.not_self", back, 409)
        if not actor.outranks(target):
            return refuse("chat.groups.error.outranked", back, 403)
        if ban:
            groups.ban(group_id, target["user_id"])
        else:
            groups.remove_member(group_id, target["user_id"])
        groups.system(group_id, "chat.system.banned" if ban else "chat.system.removed",
                      user=target["username"], actor=actor.user["username"])
    return succeed(back, "chat.groups.banned" if ban else "chat.groups.removed", values={"user": target["username"]})


@bp.route("/groups/<int:group_id>/unban", methods=["POST"])
def group_unban(group_id: int):
    back = _back(group_id)
    user_id = request.form.get("user_id") or ""
    with db.transaction():
        actor = _actor(group_id)
        if not actor.can_moderate:
            return refuse("chat.groups.error.not_allowed", back, 403)
        if user_id == actor.user["id"] and not actor.admin:
            return refuse("chat.groups.error.not_allowed", back, 403)
        row = groups.membership(group_id, user_id) if user_id else None
        if row is None or not row["banned"]:
            return refuse("chat.groups.error.not_banned", back, 404)
        account = accounts.by_id(user_id)
        name = account["username"] if account else t("common.unknown_user")
        groups.remove_member(group_id, user_id)
        groups.system(group_id, "chat.system.unbanned", user=name, actor=actor.user["username"])
    return succeed(back, "chat.groups.unbanned", values={"user": name})


@bp.route("/groups/<int:group_id>/promote", methods=["POST"])
def group_promote(group_id: int):
    return _change_role(group_id, "member", "moderator", "chat.system.promoted", "chat.groups.promoted")


@bp.route("/groups/<int:group_id>/demote", methods=["POST"])
def group_demote(group_id: int):
    return _change_role(group_id, "moderator", "member", "chat.system.demoted", "chat.groups.demoted")


def _change_role(group_id: int, current: str, new: str, system_key: str, flash_key: str):
    back = _back(group_id)
    if _throttled():
        return too_many()
    with db.transaction():
        actor = _actor(group_id)
        if not actor.can_manage:
            return refuse("chat.groups.error.owner_only", back, 403)
        target = _target(actor)
        if target is None or target["role"] != current:
            return refuse("chat.groups.error.wrong_role", back, 409)
        groups.set_role(group_id, target["user_id"], new)
        groups.system(group_id, system_key, user=target["username"], actor=actor.user["username"])
    return succeed(back, flash_key, values={"user": target["username"]})


@bp.route("/groups/<int:group_id>/self-downgrade", methods=["POST"])
def group_self_downgrade(group_id: int):
    back = _back(group_id)
    with db.transaction():
        actor = _actor(group_id)
        if actor.role != "moderator":
            key = "chat.groups.error.owner_must_transfer" if actor.is_owner else "chat.groups.error.wrong_role"
            return refuse(key, back, 409)
        groups.set_role(group_id, actor.user["id"], "member")
        groups.system(group_id, "chat.system.stepped_down", user=actor.user["username"])
    return succeed(back, "chat.groups.stepped_down")


@bp.route("/groups/<int:group_id>/transfer", methods=["POST"])
def group_transfer(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    if actor.group["is_global"]:
        return refuse("chat.groups.error.global_no_owner", back, 409)
    if not actor.is_owner:
        return refuse("chat.groups.error.owner_only", back, 403)
    target = _target(actor)
    if target is None or target["user_id"] == actor.user["id"]:
        return refuse("chat.groups.error.target_not_member", back, 404)
    try:
        with db.transaction():
            groups.transfer(group_id, actor.user["id"], target["user_id"])
            groups.system(group_id, "chat.system.transferred", user=target["username"], actor=actor.user["username"])
    except groups.TransferRefused as error:
        return refuse(error.key, back, 403)
    return succeed(back, "chat.groups.transferred", values={"user": target["username"]})


@bp.route("/groups/<int:group_id>/timeout", methods=["POST"])
def group_timeout(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.can_moderate:
        return refuse("chat.groups.error.not_allowed", back, 403)
    if _throttled():
        return too_many()
    duration = (request.form.get("duration") or "").strip()
    if duration == "indefinite":
        until, system_key, minutes = groups.INDEFINITE, "chat.system.timed_out_indefinitely", 0
    else:
        try:
            minutes = int(duration)
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= TIMEOUT_MAX_MINUTES:
            return refuse("chat.groups.error.bad_duration", back)
        until, system_key = sql_in(minutes=minutes), "chat.system.timed_out"
    with db.transaction():
        actor = _authority(group_id)
        target = _target(actor)
        if target is None:
            return refuse("chat.groups.error.target_not_member", back, 404)
        if target["user_id"] == actor.user["id"]:
            return refuse("chat.groups.error.not_self", back, 409)
        if not actor.outranks(target):
            return refuse("chat.groups.error.outranked", back, 403)
        groups.set_timeout(group_id, target["user_id"], until)
        groups.system(group_id, system_key, user=target["username"], actor=actor.user["username"], minutes=minutes)
    return succeed(back, "chat.groups.timed_out", values={"user": target["username"]})


@bp.route("/groups/<int:group_id>/untimeout", methods=["POST"])
def group_untimeout(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.can_moderate:
        return refuse("chat.groups.error.not_allowed", back, 403)
    with db.transaction():
        actor = _authority(group_id)
        target = _target(actor)
        if target is None:
            return refuse("chat.groups.error.target_not_member", back, 404)
        groups.set_timeout(group_id, target["user_id"], None)
        groups.system(group_id, "chat.system.timeout_removed", user=target["username"], actor=actor.user["username"])
    return succeed(back, "chat.groups.timeout_removed", values={"user": target["username"]})


# ── Group administration ────────────────────────────────────────────────────


@bp.route("/groups/<int:group_id>/regenerate_code", methods=["POST"])
def group_regenerate_code(group_id: int):
    """New invite code (random, or a custom one of 6-32 letters and digits); the old one stops working."""
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.can_manage:
        return refuse("chat.groups.error.owner_only", back, 403)
    if _throttled():
        return too_many()
    custom = (request.form.get("custom_code") or "").strip()
    if custom and not groups.CUSTOM_CODE.fullmatch(custom):
        return refuse("chat.groups.error.bad_custom_code", back, 400, minimum=groups.CUSTOM_CODE_MIN,
                      maximum=groups.CUSTOM_CODE_MAX)
    try:
        with db.transaction():
            actor = _authority(group_id, manage=True)
            code = groups.set_invite_code(group_id, custom or None)
            groups.system(group_id, "chat.system.code_regenerated", actor=actor.user["username"])
    except groups.CodeTaken:
        return refuse("chat.groups.error.code_taken", back, 409)
    return succeed(back, "chat.groups.code_regenerated", values={"invite": code})


@bp.route("/groups/<int:group_id>/clear", methods=["POST"])
def group_clear(group_id: int):
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.can_moderate:
        return refuse("chat.groups.error.not_allowed", back, 403)
    if _throttled():
        return too_many()
    messages, attachments = store.count(GROUP, group_id)
    groups.clear(group_id, actor.user, authorize=lambda: _authority(group_id).user)
    log.info("Group %s cleared by %s", group_id, actor.user["id"])
    return succeed(back, "chat.clear.done", values={"messages": messages, "attachments": attachments})


@bp.route("/groups/<int:group_id>/delete", methods=["POST"])
def group_delete(group_id: int):
    actor = _actor(group_id)
    if actor.group["is_global"]:
        return refuse("chat.groups.error.global_undeletable", _back(group_id), 409)
    if not actor.can_manage:
        return refuse("chat.groups.error.owner_only", _back(group_id), 403)
    groups.delete(group_id, authorize=lambda: _authority(group_id, manage=True))
    log.info("Group %s deleted by %s", group_id, actor.user["id"])
    target = url_for("chat.admin_groups") if request.form.get("from") == "admin" and actor.admin \
        else url_for("chat.group_list")
    return succeed(target, "chat.groups.deleted", values={"name": actor.group["name"]})


@bp.route("/groups/<int:group_id>/toggle_active", methods=["POST"])
@auth.admin_required
def group_toggle_active(group_id: int):
    """Pause or resume the global room (history stays readable)."""
    actor = _actor(group_id)
    back = _back(group_id)
    if not actor.group["is_global"]:
        return refuse("chat.groups.error.global_only", back, 409)
    active = not actor.group["is_active"]
    with db.transaction():
        groups.set_active(group_id, active)
        groups.system(group_id, "chat.system.reactivated" if active else "chat.system.deactivated",
                      actor=actor.user["username"])
    return succeed(back, "chat.groups.reactivated" if active else "chat.groups.deactivated")


@bp.route("/groups/<int:group_id>/admin_takeover", methods=["POST"])
@auth.admin_required
def group_admin_takeover(group_id: int):
    actor = _actor(group_id)
    with db.transaction():
        groups.take_over(group_id, actor.user["id"])
        groups.system(group_id, "chat.system.takeover", actor=actor.user["username"])
    log.info("Group %s taken over by %s", group_id, actor.user["id"])
    return succeed(_back(group_id), "chat.groups.taken_over")


@bp.route("/settings/group-badges", methods=["POST"])
def badge_toggle():
    """Show or hide one of the user's groups on their profile."""
    from . import badges

    _require_groups()
    user = auth.current_user()
    back = safe_next(url_for("chat.group_list"), request.form.get("next"))
    if not badges.enabled():
        return refuse("chat.badges.error.disabled", back, 403)
    group_id = request.form.get("group_id", type=int) or 0
    if groups.member(group_id, user["id"]) is None:
        return refuse("chat.groups.error.not_member", back, 403)
    badges.set_visible(user["id"], group_id, request.form.get("visible") == "1")
    return succeed(back, "chat.badges.saved")
