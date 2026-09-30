"""Pages of the attention feature.

* ``/attention``                        every queue waiting for the current user
* ``/attention/notices/...``            dismiss "your request was decided" notices
* ``/settings/notifications``           the account's address and email choices
* ``/account-status/notify-email``      an address for a sign-up still waiting for approval
* ``/attention/unsubscribe/<token>``    one-step opt-out from an email link (no sign-in)
* ``/admin/notifications``              email notification settings and the mail server
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from ....core import mail
from ....core.ratelimit import SqlLimiter
from ....core.web import safe_next
from ... import attention, auth, settings
from ...db import db
from . import mailer, service
from .service import NotificationError

bp = Blueprint("attention", __name__, template_folder="templates")

TEST_EMAILS_PER_HOUR = 5
PROVIDER_CHOICES = ("", *mail.PROVIDERS)


def _back(default: str):
    return redirect(safe_next(default, request.form.get("next"), request.referrer))


def _flash_error(error: NotificationError) -> None:
    auth.flash_t(error.key, "error", **error.values)


# ── The attention page and notices ───────────────────────────────────────────


@bp.get("/attention")
def index():
    user = auth.current_user()
    entries = attention.items(user, with_oldest=True)
    return render_template("attention/index.html", entries=entries, total=sum(e["count"] for e in entries),
                           notices=service.notices_for(user["id"], limit=20), notice_text=service.notice_text)


@bp.post("/attention/notices/<int:notice_id>/dismiss")
@auth.exempt("approval")
def dismiss_notice(notice_id: int):
    if not service.dismiss(auth.current_user()["id"], notice_id):
        abort(404)
    return _back(url_for("attention.index"))


@bp.post("/attention/notices/dismiss-all")
@auth.exempt("approval")
def dismiss_all():
    service.dismiss(auth.current_user()["id"])
    return _back(url_for("attention.index"))


# ── The account's own choices ────────────────────────────────────────────────


@bp.post("/settings/notifications")
def save_preferences():
    # The address receives this account's mail: not something to change while impersonating it.
    if auth.is_impersonating():
        abort(403)
    user = auth.current_user()
    try:
        service.set_email(user["id"], request.form.get("email", ""))
    except NotificationError as error:
        _flash_error(error)
        return redirect(url_for("users.settings") + "#notifications")
    attention_emails = bool(request.form.get("attention_emails")) if request.form.get("attention_choice") \
        else bool(user.get("attention_emails", 1))
    service.set_preferences(user["id"], attention_emails=attention_emails,
                            decision_emails=bool(request.form.get("decision_emails")))
    auth.flash_t("common.saved", "success")
    return redirect(url_for("users.settings") + "#notifications")


@bp.post("/account-status/notify-email")
@auth.exempt("approval", "account_steps")
def pending_email():
    """A person waiting for approval leaves an address to hear about the decision."""
    user = auth.current_user()
    if (user.get("approval_status") or "approved") != "pending":
        abort(403)
    try:
        service.set_email(user["id"], request.form.get("email", ""))
    except NotificationError as error:
        _flash_error(error)
    else:
        auth.flash_t("attention.status.saved", "success")
    return redirect(url_for("auth.account_status"))


@bp.route("/attention/unsubscribe/<token>", methods=["GET", "POST"])
@auth.public
@auth.exempt("approval", "maintenance", "account_steps")
def unsubscribe(token: str):
    found = service.read_unsubscribe_token(token)
    if found is None:
        abort(404)
    user_id, kind = found
    if request.method == "POST":
        service.unsubscribe(user_id, kind)
        return render_template("attention/unsubscribe.html", kind=kind, done=True)
    return render_template("attention/unsubscribe.html", kind=kind, done=False, token=token)


# ── Administration ────────────────────────────────────────────────────────────


def _admin_context() -> dict[str, Any]:
    return {
        "values": settings.load(),
        "modes": service.MODES,
        "providers": PROVIDER_CHOICES,
        "securities": mail.SMTP_SECURITY,
        "mail_source": service.mail_source(),
        "mail_ready": service.mail_configured(),
        "env_mail": current_app.config["BW"].mail,
        "base_url": service.base_url(),
        "base_url_from_env": bool(current_app.config["BW"].base_url),
        "suggested_base_url": request.url_root.rstrip("/"),
        "recipients": db.scalar(
            "SELECT COUNT(*) FROM users WHERE email IS NOT NULL AND email != '' AND attention_emails = 1 "
            "AND role IN ('admin', 'owner') AND suspended = 0", default=0),
        "own_email": auth.current_user().get("email") or "",
        "min_interval": service.MIN_INTERVAL,
        "max_interval": service.MAX_INTERVAL,
    }


def _int(name: str, minimum: int, maximum: int) -> int:
    value = request.form.get(name, type=int)
    if value is None or not minimum <= value <= maximum:
        raise NotificationError("attention.admin.error.number", minimum=minimum, maximum=maximum)
    return value


def _notification_values() -> dict[str, Any]:
    mode = request.form.get("attention_email_mode", "digest")
    if mode not in service.MODES:
        raise NotificationError("attention.admin.error.mode")
    values: dict[str, Any] = {
        "attention_email_enabled": 1 if request.form.get("attention_email_enabled") else 0,
        "attention_email_mode": mode,
        "attention_email_interval_minutes": _int("attention_email_interval_minutes", service.MIN_INTERVAL,
                                                 service.MAX_INTERVAL),
        "attention_email_daily_hour": _int("attention_email_daily_hour", 0, 23),
        "decision_email_enabled": 1 if request.form.get("decision_email_enabled") else 0,
    }
    if not current_app.config["BW"].base_url:
        values["public_base_url"] = service.clean_base_url(request.form.get("public_base_url", ""))
    return values


def _mail_values() -> dict[str, Any]:
    """The mail-server form; secrets are only replaced when a new value is typed."""
    provider = request.form.get("mail_provider", "")
    security = request.form.get("mail_smtp_security", "starttls")
    if provider not in PROVIDER_CHOICES or security not in mail.SMTP_SECURITY:
        raise NotificationError("attention.admin.error.mode")
    sender = request.form.get("mail_from", "").strip()
    reply_to = request.form.get("mail_reply_to", "").strip()
    for address in (sender, reply_to):
        if address and not mail.valid_address(address):
            raise NotificationError("attention.error.invalid_email")
    host = request.form.get("mail_smtp_host", "").strip()
    if len(host) > 253 or any(c.isspace() for c in host):
        raise NotificationError("attention.admin.error.host")
    values: dict[str, Any] = {
        "mail_provider": provider, "mail_from": sender, "mail_reply_to": reply_to, "mail_smtp_host": host,
        "mail_smtp_port": _int("mail_smtp_port", 1, 65535), "mail_smtp_security": security,
        "mail_smtp_username": request.form.get("mail_smtp_username", "").strip()[:200],
    }
    for secret in ("mail_smtp_password", "mail_api_key"):
        typed = request.form.get(secret, "")
        if typed or request.form.get(f"clear_{secret}"):
            values[secret] = typed.strip()[:500] if secret == "mail_api_key" else typed[:500]
    return values


@bp.route("/admin/notifications", methods=["GET", "POST"])
@auth.admin_required
def admin_settings():
    if request.method == "POST":
        try:
            if request.form.get("section") == "mail":
                if service.mail_source() != "settings":
                    abort(403)
                settings.update(_mail_values())
            else:
                settings.update(_notification_values())
        except NotificationError as error:
            _flash_error(error)
        else:
            auth.flash_t("common.saved", "success")
        return redirect(url_for("attention.admin_settings"))
    return render_template("attention/admin_settings.html", **_admin_context())


@bp.post("/admin/notifications/test")
@auth.admin_required
def send_test():
    user = auth.current_user()
    if not SqlLimiter(db.session).hit(user["id"], "attention:test_email", TEST_EMAILS_PER_HOUR, 3600):
        auth.flash_t("attention.admin.test_rate_limited", "error")
        return redirect(url_for("attention.admin_settings"))
    ok, reason = mailer.send_test(user)
    if ok:
        auth.flash_t("attention.admin.test_sent", "success", address=user["email"])
    else:
        auth.flash_t(f"attention.admin.test_failed.{reason}", "error")
    return redirect(url_for("attention.admin_settings"))
