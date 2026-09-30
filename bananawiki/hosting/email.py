"""Transactional email of the portal: Brevo or Resend (HTTPS API) or any SMTP server.

Delivery itself lives in :mod:`bananawiki.core.mail`, shared with the wiki;
this module maps the ``HOSTING_EMAIL_*`` configuration onto it. The daily
limits keep a platform inside free-tier provider quotas and stop a visitor
from exhausting them with "resend" buttons: ``HOSTING_EMAIL_DAILY_LIMIT``
messages per day in total (0 = unlimited) and 15 per recipient. Counters live
in the process, like 1.4.
"""

from __future__ import annotations

from dataclasses import replace

from ..core import mail
from ..core.mail import Message, capture_outbox
from .config import EmailSettings, HostingConfig

__all__ = ["Message", "capture_outbox", "daily_stats", "is_configured", "quota_exhausted", "send"]

BRAND = "BananaWiki Hosting"
PER_RECIPIENT_DAILY_LIMIT = 15

_limiter = mail.DailyLimiter(PER_RECIPIENT_DAILY_LIMIT)


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


def quota_exhausted(settings: EmailSettings) -> bool:
    return settings.daily_limit > 0 and _limiter.sent_today() >= settings.daily_limit


def daily_stats(settings: EmailSettings) -> tuple[int, int]:
    return _limiter.sent_today(), settings.daily_limit


def send(cfg: HostingConfig, message: Message, footer: str) -> tuple[bool, str]:
    """Send one message. Returns ``(ok, reason)``; reason is a short code, never a secret."""
    if not message.to:
        return False, "not_configured"
    message = replace(message, footer=footer, brand=BRAND, eyebrow=message.eyebrow or BRAND)
    return mail.send(mail_settings(cfg.email), message, limiter=_limiter)
