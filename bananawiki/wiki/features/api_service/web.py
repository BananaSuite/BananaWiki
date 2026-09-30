"""Browser pages: personal tokens and userbot mode, the administrator page, the API guide."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from ....core.timeutil import now_sql, to_sql
from ... import accounts, auth, settings
from ...db import db
from ...registry import feature_blueprint, is_enabled
from ...templating import from_local_input
from ..admin import service as admin_service
from . import audit, openapi, tokens, userbot, webhooks
from .errors import ApiError

bp = feature_blueprint("api_service", "api_service", __name__, template_folder="templates")

AUDIT_PAGE_SIZE = 200
DELIVERY_PAGE_SIZE = 100
# Scopes that only make sense while their feature is on (the form hides them otherwise).
FEATURE_SCOPES = {"kanban": "kanban", "canvas": "canvas"}


def _offered_scopes(user: dict[str, Any]) -> list[str]:
    return [scope for scope in tokens.scopes_for_role(user, list(tokens.SCOPES))
            if scope not in FEATURE_SCOPES or is_enabled(FEATURE_SCOPES[scope])]


# ── Personal tokens ───────────────────────────────────────────────────────────


def _tokens_page(user: dict[str, Any], *, new_token: str | None = None, new_userbot_key: str | None = None,
                 status: int = 200):
    grant_rows = [{**row, "grant": tokens.parse_grant(row["permissions"])}
                  for row in tokens.list_for_user(user["id"]) if row["name"] != tokens.USERBOT_TOKEN_NAME]
    return render_template(
        "api_service/tokens.html",
        tokens=grant_rows,
        service=tokens.service_settings(),
        has_access=tokens.has_api_access(user),
        scopes=_offered_scopes(user),
        token_count=tokens.count_active(user["id"]),
        userbot_token=userbot.token_info(user["id"]),
        user=user,
        new_token=new_token,
        new_userbot_key=new_userbot_key,
    ), status


def _no_store(result):
    body, status = result
    return body, status, {"Cache-Control": "no-store"}


def _no_impersonation() -> None:
    """An administrator viewing the wiki as someone else must not mint lasting credentials for them."""
    if auth.is_impersonating():
        abort(403)


@bp.get("/settings/api-tokens")
def tokens_page():
    return _tokens_page(auth.current_user())


@bp.get("/settings/api")
@bp.get("/account/api")
def legacy_tokens_page():
    return redirect(url_for("api_service.tokens_page"), code=301)


@bp.post("/settings/api-tokens/create")
def create_token():
    """Show the new token once, in this response only (it is never put in a cookie)."""
    _no_impersonation()
    user = auth.current_user()
    service = tokens.service_settings()
    if not service["enabled"]:
        auth.flash_t("api_service.flash.service_disabled", "error")
    elif not tokens.has_api_access(user):
        auth.flash_t("api_service.flash.no_api_access", "error")
    else:
        expires_at = None
        raw_expiry = (request.form.get("expires_at") or "").strip()
        if raw_expiry:
            moment = from_local_input(raw_expiry)
            expires_at = to_sql(moment) if moment else None
            if expires_at is None:
                auth.flash_t("api_service.flash.invalid_expiry", "error")
                return redirect(url_for("api_service.tokens_page"))
            if expires_at <= now_sql():
                auth.flash_t("api_service.flash.expiry_in_past", "error")
                return redirect(url_for("api_service.tokens_page"))
        try:
            tokens.check_quota(user["id"])
        except ApiError as error:
            auth.flash_t("api_service.error.token_limit", "error", **error.values)
            return redirect(url_for("api_service.tokens_page"))
        grant = tokens.web_grant(user, request.form.getlist("scopes"), read_only=bool(request.form.get("read_only")))
        raw, _token_id = tokens.create(user["id"], name=request.form.get("name", ""), grant=grant,
                                       expires_at=expires_at)
        return _no_store(_tokens_page(user, new_token=raw, status=201))
    return redirect(url_for("api_service.tokens_page"))


@bp.post("/settings/api-tokens/<int:token_id>/revoke")
def revoke_token(token_id: int):
    user = auth.current_user()
    token = tokens.get(token_id)
    if token is None or token["user_id"] != user["id"]:
        auth.flash_t("api_service.flash.token_not_found", "error")
    else:
        tokens.revoke(token_id)
        auth.flash_t("api_service.flash.token_revoked", "success")
    return redirect(url_for("api_service.tokens_page"))


@bp.post("/settings/api-tokens/userbot")
def userbot_mode():
    _no_impersonation()
    user = auth.current_user()
    try:
        if request.form.get("enable") == "1":
            key = userbot.enable(user)
            auth.refresh_current_user()
            return _no_store(_tokens_page(auth.current_user(), new_userbot_key=key))
        userbot.disable(user)
        auth.flash_t("api_service.userbot.disabled", "success")
    except userbot.UserbotError as error:
        auth.flash_t(error.key, "error")
    return redirect(url_for("api_service.tokens_page", _anchor="userbot"))


def account_settings_section() -> str:
    """Slot ``account.settings_sections``: a summary with a link to the token page."""
    user = auth.current_user()
    if not user:
        return ""
    return render_template("api_service/_settings_section.html", service=tokens.service_settings(),
                           has_access=tokens.has_api_access(user), token_count=tokens.count_active(user["id"]))


# ── Administration ────────────────────────────────────────────────────────────


def _admin_redirect(anchor: str | None = None):
    return redirect(url_for("api_service.admin", _anchor=anchor))


def _admin_page(*, new_secret: dict[str, Any] | None = None, status: int = 200):
    users = db.all("SELECT id, username, role, api_access_enabled, userbot_enabled, userbot_mode_lock, "
                   "is_superuser FROM users ORDER BY username COLLATE NOCASE")
    token_rows = [{**row, "grant": tokens.parse_grant(row["permissions"])} for row in tokens.list_all()]
    return render_template(
        "api_service/admin.html",
        service=tokens.service_settings(),
        tokens=token_rows,
        users=users,
        entries=audit.entries(limit=AUDIT_PAGE_SIZE),
        total_entries=audit.count(),
        bounds={"rate": tokens.RATE_LIMIT_BOUNDS, "tokens": tokens.MAX_TOKENS_BOUNDS,
                "failures": webhooks.MAX_FAILURES_BOUNDS},
        webhook_max_failures=webhooks.max_failures(),
        auto_disable_days=webhooks.AUTO_DISABLE_DAYS,
        lock_modes=userbot.LOCK_MODES,
        webhooks=[{**hook, "event_list": webhooks.subscribed_events(hook)} for hook in webhooks.all_webhooks()],
        webhook_events=list(webhooks.EVENTS),
        max_webhooks=webhooks.MAX_WEBHOOKS,
        private_network_offered=webhooks.private_network_offered(),
        new_secret=new_secret,
    ), status


@bp.get("/admin/api-service")
@auth.admin_required
def admin():
    return _admin_page()


def _form_int(name: str, default: int, bounds: tuple[int, int]) -> int:
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(bounds[0], min(bounds[1], value))


@bp.post("/admin/api-service/settings")
@auth.admin_required
def admin_settings():
    settings.update({
        "api_service_enabled": 1 if request.form.get("enabled") else 0,
        "api_service_rate_limit": _form_int("rate_limit", 60, tokens.RATE_LIMIT_BOUNDS),
        "api_service_admin_rate_limit": _form_int("admin_rate_limit", 120, tokens.RATE_LIMIT_BOUNDS),
        "api_service_max_tokens_per_user": _form_int("max_tokens_per_user", 5, tokens.MAX_TOKENS_BOUNDS),
        "api_service_webhook_max_failures": _form_int("webhook_max_failures", webhooks.MAX_FAILURES_DEFAULT,
                                                      webhooks.MAX_FAILURES_BOUNDS),
    })
    auth.flash_t("api_service.flash.settings_saved", "success")
    return _admin_redirect()


def _protection_error(target: dict[str, Any]) -> str | None:
    """Why the current administrator may not act on *target*'s account (the hierarchy of the admin pages)."""
    return admin_service.protection_error(auth.current_user(), target)


@bp.post("/admin/api-service/tokens/<int:token_id>/revoke")
@auth.admin_required
def admin_revoke_token(token_id: int):
    token = tokens.get(token_id)
    owner = accounts.by_id(token["user_id"]) if token else None
    if token is None:
        auth.flash_t("api_service.flash.token_not_found", "error")
    elif owner and (error := _protection_error(owner)):
        auth.flash_t(error, "error")
    else:
        tokens.revoke(token_id)
        auth.flash_t("api_service.flash.token_revoked", "success")
    return _admin_redirect("tokens")


@bp.post("/admin/api-service/tokens/revoke-all")
@auth.admin_required
def admin_revoke_all():
    target = accounts.by_id(request.form.get("user_id", "").strip())
    if target is None:
        auth.flash_t("api_service.flash.user_not_found", "error")
    elif error := _protection_error(target):
        auth.flash_t(error, "error")
    else:
        count = tokens.revoke_all_for_user(target["id"])
        auth.flash_t("api_service.flash.all_revoked", "success", username=target["username"], count=count)
    return _admin_redirect("users")


@bp.post("/admin/api-service/users/<user_id>/toggle-access")
@auth.admin_required
def admin_toggle_access(user_id: str):
    target = accounts.by_id(user_id)
    if target is None:
        auth.flash_t("api_service.flash.user_not_found", "error")
    elif target["role"] == "owner" or target.get("is_superuser"):
        auth.flash_t("api_service.flash.owner_protected", "error")
    else:
        enabled = not target.get("api_access_enabled")
        db.execute("UPDATE users SET api_access_enabled = ? WHERE id = ?", (1 if enabled else 0, user_id))
        auth.flash_t("api_service.flash.access_enabled" if enabled else "api_service.flash.access_disabled",
                     "success", username=target["username"])
    return _admin_redirect("users")


@bp.post("/admin/api-service/users/<user_id>/userbot-lock")
@auth.admin_required
def admin_userbot_lock(user_id: str):
    target = accounts.by_id(user_id)
    if target is None:
        auth.flash_t("api_service.flash.user_not_found", "error")
    elif error := _protection_error(target):
        auth.flash_t(error, "error")
    else:
        try:
            userbot.set_lock(target, request.form.get("mode", ""))
        except userbot.UserbotError as error:
            auth.flash_t(error.key, "error")
        else:
            auth.flash_t("api_service.userbot.lock_saved", "success", username=target["username"])
    return _admin_redirect("users")


@bp.post("/admin/api-service/clear-audit")
@auth.admin_required
def admin_clear_audit():
    if not auth.current_user().get("is_superuser"):
        auth.flash_t("api_service.flash.superuser_only", "error")
        return _admin_redirect("audit")
    days = _form_int("before_days", 90, (1, 3650))
    count = audit.clear(days)
    auth.flash_t("api_service.flash.audit_cleared", "success", days=days, count=count)
    return _admin_redirect("audit")


# ── Webhooks ──────────────────────────────────────────────────────────────────


def _webhook_or_none(webhook_id: int) -> dict[str, Any] | None:
    hook = webhooks.get(webhook_id)
    if hook is None:
        auth.flash_t("api_service.flash.webhook_not_found", "error")
    return hook


def _flash_error(error: ApiError) -> None:
    auth.flash_t(error.message_key, "error", **error.values)


@bp.post("/admin/api-service/webhooks")
@auth.admin_required
def admin_create_webhook():
    """Show the new secret once, in this response only."""
    try:
        hook, secret = webhooks.create(
            url=request.form.get("url", ""), events=request.form.getlist("events"),
            description=request.form.get("description", ""),
            allow_private=bool(request.form.get("allow_private_network")),
            created_by=auth.current_user()["id"],
        )
    except ApiError as error:
        _flash_error(error)
        return _admin_redirect("webhooks")
    return _no_store(_admin_page(new_secret={"id": hook["id"], "url": hook["url"], "secret": secret}, status=201))


@bp.post("/admin/api-service/webhooks/<int:webhook_id>/toggle")
@auth.admin_required
def admin_toggle_webhook(webhook_id: int):
    hook = _webhook_or_none(webhook_id)
    if hook is not None:
        webhooks.update(hook, {"active": not hook["active"]})
        auth.flash_t("api_service.flash.webhook_disabled" if hook["active"] else "api_service.flash.webhook_enabled",
                     "success")
    return _admin_redirect("webhooks")


@bp.post("/admin/api-service/webhooks/<int:webhook_id>/ping")
@auth.admin_required
def admin_ping_webhook(webhook_id: int):
    hook = _webhook_or_none(webhook_id)
    if hook is not None:
        webhooks.ping(hook, actor_id=auth.current_user()["id"])
        auth.flash_t("api_service.flash.webhook_ping_queued", "success")
        return redirect(url_for("api_service.admin_webhook", webhook_id=webhook_id))
    return _admin_redirect("webhooks")


@bp.post("/admin/api-service/webhooks/<int:webhook_id>/rotate")
@auth.admin_required
def admin_rotate_webhook(webhook_id: int):
    hook = _webhook_or_none(webhook_id)
    if hook is None:
        return _admin_redirect("webhooks")
    secret = webhooks.rotate_secret(hook)
    return _no_store(_admin_page(new_secret={"id": hook["id"], "url": hook["url"], "secret": secret}))


@bp.post("/admin/api-service/webhooks/<int:webhook_id>/delete")
@auth.admin_required
def admin_delete_webhook(webhook_id: int):
    hook = _webhook_or_none(webhook_id)
    if hook is not None:
        webhooks.delete(hook)
        auth.flash_t("api_service.flash.webhook_deleted", "success")
    return _admin_redirect("webhooks")


@bp.get("/admin/api-service/webhooks/<int:webhook_id>")
@auth.admin_required
def admin_webhook(webhook_id: int):
    hook = webhooks.get(webhook_id)
    if hook is None:
        abort(404)
    return render_template("api_service/webhook.html", hook=hook, events=webhooks.subscribed_events(hook),
                           deliveries=webhooks.deliveries(webhook_id, limit=DELIVERY_PAGE_SIZE, offset=0))


@bp.post("/admin/api-service/webhooks/<int:webhook_id>/deliveries/<int:delivery_id>/redeliver")
@auth.admin_required
def admin_redeliver(webhook_id: int, delivery_id: int):
    row = webhooks.get_delivery(webhook_id, delivery_id)
    if row is None or row["state"] == "pending":
        auth.flash_t("api_service.flash.delivery_not_redeliverable", "error")
    else:
        webhooks.redeliver(row)
        auth.flash_t("api_service.flash.delivery_queued", "success")
    return redirect(url_for("api_service.admin_webhook", webhook_id=webhook_id))


# ── Guide ─────────────────────────────────────────────────────────────────────


@bp.get("/api-docs")
@auth.public
def docs():
    groups: dict[str, list[openapi.Endpoint]] = {}
    for endpoint in openapi.ENDPOINTS:
        groups.setdefault(endpoint.group, []).append(endpoint)
    return render_template("api_service/docs.html", groups=groups, scopes=tokens.SCOPES,
                           example_url=request.url_root.rstrip("/"))
