"""Transactional email of the portal: Brevo or Resend (HTTPS API) or any SMTP server.

Delivery itself lives in :mod:`bananawiki.core.mail`, shared with the wiki;
this module maps the ``HOSTING_EMAIL_*`` configuration onto it and keeps the
platform inside its provider's quota. Every message sent is logged in
``hosting_email_log`` and the budgets count the last 24 hours of that log,
so they hold across workers and restarts:

* ``HOSTING_EMAIL_DAILY_LIMIT`` messages in total (0 = unlimited), in two
  parts that cannot use each other up: a fifth for account recovery
  (password resets and user-name reminders, which anyone can ask for
  without signing in) and the rest for everything else, verification links
  included. User-name reminders, which need only an address, may use half
  of the recovery part; password resets, which need the user name too, may
  use all of it.
* 15 messages per recipient, of which 3 password resets and 3 user-name
  reminders (counted apart, so reminders cannot hold back a reset).
* ``HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT`` messages an account causes itself
  (verification links when it changes its address, notices about its own
  actions), so one account cannot spend the platform's budget or mail
  arbitrary addresses.
* ``RECOVERY_REQUESTS_PER_NETWORK`` recovery requests per client network
  (an IPv4 address or an IPv6 /64) and ``RECOVERY_REQUESTS_PER_IPV6_SITE``
  per IPv6 /48, counted by the recovery forms whether or not an account
  matches, so one client cannot spend the recovery part.

A refused message is reported to the caller (``daily_limit``,
``recipient_limit`` or ``account_limit``), never queued; a message the
provider did not accept does not count. Once the recovery part (or the
whole budget) is spent, the recovery forms say so to everyone alike
(:func:`recovery_paused`) and administrators see it among the things that
need them (:mod:`.attention`).
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..core import mail
from ..core.mail import Message, capture_outbox
from ..core.timeutil import now_sql, sql_in
from .config import EmailSettings, HostingConfig
from .db import db

__all__ = ["Message", "capture_outbox", "daily_stats", "is_configured", "quota_exhausted", "send"]

log = logging.getLogger("bananawiki.hosting.email")

PER_RECIPIENT_DAILY_LIMIT = 15
RECOVERY_KINDS = frozenset({"reset", "username"})
RECOVERY_RESERVE_PERCENT = 20
# User-name reminders may use this share of the recovery part (password resets may use all of it).
REMINDER_SHARE_PERCENT = 50
# Recovery messages of each kind one address receives a day.
RECOVERY_PER_RECIPIENT_DAILY_LIMIT = 3
RECOVERY_REQUESTS_PER_NETWORK = 10
RECOVERY_REQUESTS_PER_IPV6_SITE = 3 * RECOVERY_REQUESTS_PER_NETWORK
LOG_RETENTION_DAYS = 2
_KINDS = ", ".join(f"'{kind}'" for kind in sorted(RECOVERY_KINDS))
_RECOVERY_SQL, _OTHER_SQL = f" AND kind IN ({_KINDS})", f" AND kind NOT IN ({_KINDS})"


def mail_settings(settings: EmailSettings) -> mail.MailSettings:
    """The core mail settings for the portal's ``HOSTING_EMAIL_*`` configuration."""
    if not settings.smtp_tls:
        security = "none"
    else:
        security = "ssl" if settings.smtp_port == 465 else "starttls"
    return mail.MailSettings(
        provider=settings.provider, sender=settings.sender, reply_to=settings.reply_to, api_key=settings.api_key,
        smtp_host=settings.smtp_host, smtp_port=settings.smtp_port, smtp_username=settings.smtp_username,
        smtp_password=settings.smtp_password, smtp_security=security, daily_limit=settings.daily_limit,
    )


def is_configured(settings: EmailSettings) -> bool:
    return mail.is_configured(mail_settings(settings))


def recovery_reserve(daily_limit: int) -> int:
    """Messages of the daily budget kept for account recovery, which may use nothing else."""
    return max(1, daily_limit * RECOVERY_RESERVE_PERCENT // 100) if daily_limit > 0 else 0


def reminder_allowance(daily_limit: int) -> int:
    """Messages of the recovery part that user-name reminders may use."""
    reserve = recovery_reserve(daily_limit)
    return max(1, reserve * REMINDER_SHARE_PERCENT // 100) if reserve else 0


def _sent_since(where: str = "", params: tuple = ()) -> int:
    return int(db.scalar(f"SELECT COUNT(*) FROM hosting_email_log WHERE sent_at > ?{where}",
                         (sql_in(days=-1), *params), default=0))


def quota_exhausted(settings: EmailSettings) -> bool:
    return settings.daily_limit > 0 and _sent_since() >= settings.daily_limit


def daily_stats(settings: EmailSettings) -> tuple[int, int]:
    return _sent_since(), settings.daily_limit


def recovery_paused(settings: EmailSettings, kind: str) -> bool:
    """Whether no recovery message of *kind* can go out to anyone today (the platform's share is spent).

    It does not depend on any account, so the recovery forms can say so.
    """
    if settings.daily_limit <= 0:
        return False
    if _sent_since() >= settings.daily_limit or _sent_since(_RECOVERY_SQL) >= recovery_reserve(settings.daily_limit):
        return True
    return kind == "username" and _sent_since(" AND kind = 'username'") >= reminder_allowance(settings.daily_limit)


def refusal(settings: EmailSettings, recipient: str, kind: str, account_id: str | None) -> str | None:
    """Why one more message to *recipient* may not go out now, or None."""
    recovery = kind in RECOVERY_KINDS
    if _sent_since(" AND recipient = ?", (recipient.lower(),)) >= PER_RECIPIENT_DAILY_LIMIT:
        return "recipient_limit"
    if recovery and (_sent_since(" AND recipient = ? AND kind = ?", (recipient.lower(), kind))
                     >= RECOVERY_PER_RECIPIENT_DAILY_LIMIT):
        return "recipient_limit"
    if (account_id and settings.account_daily_limit
            and _sent_since(" AND account_id = ?", (account_id,)) >= settings.account_daily_limit):
        return "account_limit"
    if settings.daily_limit > 0:
        if recovery:
            if recovery_paused(settings, kind):
                return "daily_limit"
        elif (_sent_since(_OTHER_SQL) >= settings.daily_limit - recovery_reserve(settings.daily_limit)
              or _sent_since() >= settings.daily_limit):
            return "daily_limit"
    return None


def prune() -> int:
    """Forget messages older than the budgets look back (maintenance)."""
    return db.execute("DELETE FROM hosting_email_log WHERE sent_at < ?",
                      (sql_in(days=-LOG_RETENTION_DAYS),)).rowcount


def send(cfg: HostingConfig, message: Message, footer: str, *, kind: str = "",
         account_id: str | None = None) -> tuple[bool, str]:
    """Send one message within the budgets. Returns ``(ok, reason)``; reason is a short code, never a secret.

    *kind* is the message's ``email.<kind>`` key (recovery kinds may use the
    reserve); *account_id* is the account that caused it, charged to that
    account's own budget.
    """
    if not message.to:
        return False, "not_configured"
    settings = mail_settings(cfg.email)
    if not mail.is_configured(settings):
        return False, "not_configured"
    try:
        recipient = mail.normalize_address(message.to)
    except mail.MailError as error:
        return False, error.code
    message = replace(message, to=recipient, footer=footer, brand=cfg.service_name,
                      eyebrow=message.eyebrow or cfg.service_name)
    with db.transaction():  # BEGIN IMMEDIATE: concurrent workers count one after the other
        reason = refusal(cfg.email, recipient, kind, account_id)
        if reason:
            log.warning("Email %s to %s not sent: %s", kind or "message", recipient, reason)
            return False, reason
        entry = db.insert("hosting_email_log", {"sent_at": now_sql(), "recipient": recipient.lower(),
                                                "account_id": account_id, "kind": kind[:64]})
    ok, reason = mail.send(settings, message)
    if not ok:
        db.execute("DELETE FROM hosting_email_log WHERE id = ?", (entry,))
    return ok, reason
