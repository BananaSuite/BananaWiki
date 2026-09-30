"""Notices, notification preferences and the wiki's outgoing-mail configuration.

Mail settings come from the operator's environment (``BW_MAIL_*`` /
``BW_SMTP_*``, see :func:`bananawiki.core.mail.settings_from_env`) when set,
otherwise from the administrator's settings (secrets encrypted at rest).
Under managed hosting only the environment counts: the host decides which
mail server tenant wikis may use.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from ....core import attention as engine
from ....core import mail
from ....core.timeutil import now_sql, sql_in
from ... import attention, settings
from ...db import db

MODES = engine.MODES
NOTICE_KINDS = ("attention", "decisions")
MAX_NOTICES_SHOWN = 3
NOTICE_RETENTION_DAYS = 90
MIN_INTERVAL, MAX_INTERVAL = 5, 1440


class NotificationError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Mail configuration ────────────────────────────────────────────────────────


def mail_source() -> str:
    """``env`` (operator), ``settings`` (administrator) or ``host_only`` (managed, not provided)."""
    cfg = current_app.config["BW"]
    if cfg.mail is not None:
        return "env"
    return "host_only" if cfg.managed_hosting else "settings"


def mail_settings() -> mail.MailSettings | None:
    source = mail_source()
    if source == "env":
        return current_app.config["BW"].mail
    if source == "host_only":
        return None
    s = settings.load()
    try:
        port = int(s.get("mail_smtp_port") or 587)
    except (TypeError, ValueError):
        port = 587
    return mail.MailSettings(
        provider=str(s.get("mail_provider") or ""), sender=str(s.get("mail_from") or ""),
        reply_to=str(s.get("mail_reply_to") or ""), api_key=str(s.get("mail_api_key") or ""),
        smtp_host=str(s.get("mail_smtp_host") or ""), smtp_port=port,
        smtp_username=str(s.get("mail_smtp_username") or ""), smtp_password=str(s.get("mail_smtp_password") or ""),
        smtp_security=str(s.get("mail_smtp_security") or "starttls"),
    )


def mail_configured() -> bool:
    return mail.is_configured(mail_settings())


def base_url() -> str:
    """The wiki's public address for links in emails ('' when unknown)."""
    return current_app.config["BW"].base_url or str(settings.get("public_base_url") or "").rstrip("/")


def clean_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password \
            or parts.query or parts.fragment or len(value) > 300:
        raise NotificationError("attention.admin.error.base_url")
    return value


def policy() -> engine.Policy:
    from ...templating import site_timezone

    mode = str(settings.get("attention_email_mode") or "digest")
    return engine.Policy(
        mode=mode if mode in MODES else "digest",
        interval_minutes=_bounded(settings.get("attention_email_interval_minutes"), 60, MIN_INTERVAL, MAX_INTERVAL),
        daily_hour=_bounded(settings.get("attention_email_daily_hour"), 8, 0, 23),
        timezone=site_timezone(),
    )


def _bounded(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(value)))
    except (TypeError, ValueError):
        return default


# ── People's addresses and choices ───────────────────────────────────────────


def set_email(user_id: str, address: str) -> str:
    """Store (or clear, with '') the notification address of an account."""
    address = (address or "").strip()
    if address:
        try:
            address = mail.normalize_address(address)
        except mail.MailError:
            raise NotificationError("attention.error.invalid_email") from None
    db.execute("UPDATE users SET email = ? WHERE id = ?", (address or None, user_id))
    return address


def set_preferences(user_id: str, *, attention_emails: bool, decision_emails: bool) -> None:
    db.execute("UPDATE users SET attention_emails = ?, decision_emails = ? WHERE id = ?",
               (1 if attention_emails else 0, 1 if decision_emails else 0, user_id))


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["BW"].secret_key, salt="bananawiki.attention.unsubscribe")


def unsubscribe_token(user_id: str, kind: str) -> str:
    return _serializer().dumps({"u": user_id, "k": kind})


def read_unsubscribe_token(token: str) -> tuple[str, str] | None:
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict) or data.get("k") not in NOTICE_KINDS or not isinstance(data.get("u"), str):
        return None
    return data["u"], data["k"]


def unsubscribe(user_id: str, kind: str) -> bool:
    column = "attention_emails" if kind == "attention" else "decision_emails"
    return bool(db.execute(f"UPDATE users SET {column} = 0 WHERE id = ?", (user_id,)).rowcount)


# ── Events ────────────────────────────────────────────────────────────────────


def store() -> engine.Store:
    return engine.Store(db.session, "attention_events", "attention_recipients")


def on_created(source: str, object_id: str = "") -> None:
    """``attention.created``: remember the new item so the next email run reacts at once."""
    store().record_event(source, object_id)


def on_user_created(user: dict[str, Any]) -> None:
    """``user.created``: a sign-up waiting for approval is a new item in ``admin.signups``."""
    if (user or {}).get("approval_status") == "pending":
        attention.created("admin.signups", user["id"])


def on_decided(user_id: str, source: str, outcome: str, object_id: str = "", endpoint: str = "") -> None:
    """``attention.decided``: an in-app notice for the person who asked (emailed by the job)."""
    db.insert("user_notices", {"user_id": user_id, "source_id": source[:100], "outcome": outcome,
                               "object_id": object_id[:100], "endpoint": endpoint[:100], "created_at": now_sql()})


# ── Notices ───────────────────────────────────────────────────────────────────


def notices_for(user_id: str, limit: int = MAX_NOTICES_SHOWN) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM user_notices WHERE user_id = ? AND dismissed_at IS NULL "
                  "ORDER BY id DESC LIMIT ?", (user_id, limit))


def dismiss(user_id: str, notice_id: int | None = None) -> int:
    if notice_id is None:
        return db.execute("UPDATE user_notices SET dismissed_at = ? WHERE user_id = ? AND dismissed_at IS NULL",
                          (now_sql(), user_id)).rowcount
    return db.execute("UPDATE user_notices SET dismissed_at = ? WHERE id = ? AND user_id = ? "
                      "AND dismissed_at IS NULL", (now_sql(), notice_id, user_id)).rowcount


def notice_text(notice: dict[str, Any], lang: str | None = None) -> str:
    """"Your quota request was approved." in the viewer's (or *lang*) language."""
    from ...i18n import t, t_lang

    translate = (lambda key, default=None: t_lang(lang, key, default)) if lang else t
    generic = translate(f"attention.notice.generic.{notice['outcome']}")
    return translate(f"attention.notice.{notice['source_id']}.{notice['outcome']}", generic)


def notice_url_endpoint(notice: dict[str, Any]) -> str:
    return notice.get("endpoint") or "attention.index"


def notice_url(notice: dict[str, Any]) -> str:
    """Where the person can see the decided request ('' when that page no longer exists)."""
    from flask import url_for

    try:
        return url_for(notice_url_endpoint(notice))
    except Exception:  # noqa: BLE001 - an endpoint of a feature that is gone
        return ""


def prune_notices() -> int:
    return db.execute("DELETE FROM user_notices WHERE created_at < ?", (sql_in(days=-NOTICE_RETENTION_DAYS),)).rowcount
