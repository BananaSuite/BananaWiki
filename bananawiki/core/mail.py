"""Outgoing email shared by the wiki and the hosting portal.

Providers
---------
* ``smtp``   - any SMTP server, with STARTTLS (``starttls``, the default),
  implicit TLS (``ssl``, usually port 465) or no encryption (``none``, only
  accepted for a server on this machine when a password is sent);
* ``brevo`` / ``resend`` - the HTTPS APIs of those services (API key).

A :class:`Message` carries plain text plus a few structured parts (title,
call-to-action link, one highlighted value, footer, unsubscribe link); the
HTML version is rendered here with inline styles so every mail client shows
it the same way. Header values never contain line breaks (header injection
is refused, not silently repaired) and addresses are validated before any
connection is made.

:func:`capture_outbox` collects messages instead of sending them (tests).
:class:`DailyLimiter` keeps a sender inside a provider's free-tier quota.
"""

from __future__ import annotations

import html
import json
import logging
import re
import smtplib
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .env import ConfigError, Env

log = logging.getLogger("bananawiki.mail")

PROVIDERS = ("smtp", "brevo", "resend")
SMTP_SECURITY = ("starttls", "ssl", "none")
MAX_ADDRESS = 254
MAX_SUBJECT = 200
DEFAULT_TIMEOUT = 15
_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_URL = re.compile(r"https?://[^\s<>]+")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_PROVIDER_URLS = {"brevo": "https://api.brevo.com/v3/smtp/email", "resend": "https://api.resend.com/emails"}


@dataclass(frozen=True)
class MailSettings:
    """How to reach the mail provider. Secrets never appear in ``repr``."""

    provider: str = ""
    sender: str = ""
    reply_to: str = ""
    api_key: str = field(default="", repr=False)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_security: str = "starttls"
    timeout: int = DEFAULT_TIMEOUT
    daily_limit: int = 0  # 0 = unlimited


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    title: str = ""
    eyebrow: str = ""
    action_url: str = ""
    action_label: str = ""
    detail_label: str = ""
    detail_value: str = ""
    footer: str = ""
    brand: str = "BananaWiki"
    unsubscribe_url: str = ""


class MailError(ValueError):
    """A message or setting that cannot be sent as it is (short reason code in ``code``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── Validation ────────────────────────────────────────────────────────────────


def valid_address(value: str | None) -> bool:
    """A single plain address such as ``name@example.org`` (no display name, no line breaks)."""
    text = str(value or "")
    return 3 <= len(text) <= MAX_ADDRESS and bool(_ADDRESS.match(text))


def normalize_address(value: str | None) -> str:
    """Trim and lower-case the domain; raise :class:`MailError` when invalid."""
    text = str(value or "").strip()
    if not valid_address(text):
        raise MailError("invalid_address")
    local, _, domain = text.rpartition("@")
    return f"{local}@{domain.lower()}"


def _sender_address(value: str) -> str:
    """The address part of ``Name <addr>`` or a bare address ('' when invalid)."""
    if "\r" in value or "\n" in value:
        return ""
    address = parseaddr(value)[1]
    return address if valid_address(address) else ""


def header_value(value: str, *, limit: int = MAX_SUBJECT) -> str:
    """A header value without line breaks; raise :class:`MailError` on injection attempts."""
    text = str(value or "")
    if "\r" in text or "\n" in text or "\x00" in text:
        raise MailError("invalid_header")
    return text[:limit]


def is_configured(settings: MailSettings | None) -> bool:
    if _outbox is not None:
        return True
    if settings is None or not _sender_address(settings.sender):
        return False
    if settings.provider in ("brevo", "resend"):
        return bool(settings.api_key)
    if settings.provider == "smtp":
        return bool(settings.smtp_host)
    return False


def settings_from_env(env: Env, prefix: str) -> MailSettings | None:
    """Mail settings from ``<prefix>MAIL_*`` / ``<prefix>SMTP_*``; None when none are set.

    ``BW_MAIL_PROVIDER`` defaults to ``smtp`` when ``BW_SMTP_HOST`` is set.
    """
    host = env.str(f"{prefix}SMTP_HOST")
    provider = env.str(f"{prefix}MAIL_PROVIDER").lower() or ("smtp" if host else "")
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ConfigError(f"{prefix}MAIL_PROVIDER must be one of {', '.join(PROVIDERS)}.")
    sender = env.str(f"{prefix}MAIL_FROM")
    if sender and not _sender_address(sender):
        raise ConfigError(f"{prefix}MAIL_FROM must be an email address (optionally 'Name <address>').")
    port = env.int(f"{prefix}SMTP_PORT", 587, minimum=1, maximum=65535)
    security = env.str(f"{prefix}SMTP_SECURITY").lower() or ("ssl" if port == 465 else "starttls")
    if security not in SMTP_SECURITY:
        raise ConfigError(f"{prefix}SMTP_SECURITY must be one of {', '.join(SMTP_SECURITY)}.")
    return MailSettings(
        provider=provider, sender=sender, reply_to=env.str(f"{prefix}MAIL_REPLY_TO"),
        api_key=env.str(f"{prefix}MAIL_API_KEY"), smtp_host=host, smtp_port=port,
        smtp_username=env.str(f"{prefix}SMTP_USERNAME"), smtp_password=env.raw(f"{prefix}SMTP_PASSWORD") or "",
        smtp_security=security, timeout=env.int(f"{prefix}MAIL_TIMEOUT", DEFAULT_TIMEOUT, minimum=1, maximum=120),
        daily_limit=env.int(f"{prefix}MAIL_DAILY_LIMIT", 0, minimum=0),
    )


# ── Quotas ────────────────────────────────────────────────────────────────────


class DailyLimiter:
    """Per-process daily counters: a total and a per-recipient budget."""

    def __init__(self, per_recipient: int = 15) -> None:
        self.per_recipient = per_recipient
        self._lock = threading.Lock()
        self._day = date.today()
        self._total = 0
        self._recipients: dict[str, int] = {}

    def allow(self, recipient: str, daily_limit: int) -> str | None:
        """Count one message; return a refusal reason or None."""
        recipient = recipient.lower()
        with self._lock:
            today = date.today()
            if today != self._day:
                self._day, self._total, self._recipients = today, 0, {}
            if self.per_recipient and self._recipients.get(recipient, 0) >= self.per_recipient:
                return "recipient_limit"
            if daily_limit > 0 and self._total >= daily_limit:
                return "daily_limit"
            self._total += 1
            self._recipients[recipient] = self._recipients.get(recipient, 0) + 1
            return None

    def sent_today(self) -> int:
        with self._lock:
            return self._total if self._day == date.today() else 0


# ── Test outbox ───────────────────────────────────────────────────────────────

_outbox: list[Message] | None = None


@contextmanager
def capture_outbox() -> Iterator[list[Message]]:
    """Within the block, send nothing and collect the messages instead (tests)."""
    global _outbox
    previous, _outbox = _outbox, []
    try:
        yield _outbox
    finally:
        _outbox = previous


# ── Sending ───────────────────────────────────────────────────────────────────


def send(settings: MailSettings | None, message: Message, *, limiter: DailyLimiter | None = None) -> tuple[bool, str]:
    """Send one message. Returns ``(ok, reason)``; *reason* is a short code, never a secret."""
    if not is_configured(settings):
        return False, "not_configured"
    try:
        message = replace(message, to=normalize_address(message.to), subject=header_value(message.subject))
    except MailError as error:
        return False, error.code
    if limiter is not None:
        refusal = limiter.allow(message.to, settings.daily_limit if settings else 0)
        if refusal:
            log.warning("Email to %s not sent: %s", message.to, refusal)
            return False, refusal
    body_html = render_html(message)
    if _outbox is not None:
        _outbox.append(message)
        return True, ""
    assert settings is not None
    try:
        if settings.provider == "brevo":
            return _brevo(settings, message, body_html)
        if settings.provider == "resend":
            return _resend(settings, message, body_html)
        return _smtp(settings, message, body_html)
    except MailError as error:
        return False, error.code


def _reply_to(settings: MailSettings) -> tuple[str, str]:
    name, address = parseaddr(header_value(settings.reply_to, limit=MAX_ADDRESS * 2))
    return name, address if valid_address(address) else ""


def _brevo(settings: MailSettings, message: Message, body_html: str) -> tuple[bool, str]:
    sender_name, sender = parseaddr(settings.sender)
    reply_name, reply = _reply_to(settings)
    payload: dict = {
        "sender": {"name": sender_name or message.brand, "email": sender},
        "to": [{"email": message.to}], "subject": message.subject,
        "textContent": message.text, "htmlContent": body_html,
    }
    if reply:
        payload["replyTo"] = {"email": reply, **({"name": reply_name} if reply_name else {})}
    if message.unsubscribe_url:
        payload["headers"] = {"List-Unsubscribe": f"<{message.unsubscribe_url}>"}
    return _post(_PROVIDER_URLS["brevo"], {"api-key": settings.api_key}, payload, settings.timeout)


def _resend(settings: MailSettings, message: Message, body_html: str) -> tuple[bool, str]:
    payload: dict = {"from": settings.sender, "to": [message.to], "subject": message.subject,
                     "text": message.text, "html": body_html}
    reply = _reply_to(settings)[1]
    if reply:
        payload["reply_to"] = reply
    if message.unsubscribe_url:
        payload["headers"] = {"List-Unsubscribe": f"<{message.unsubscribe_url}>"}
    return _post(_PROVIDER_URLS["resend"], {"Authorization": f"Bearer {settings.api_key}"}, payload,
                 settings.timeout)


def _post(url: str, headers: dict[str, str], payload: dict, timeout: int) -> tuple[bool, str]:
    request = urllib.request.Request(  # noqa: S310 - fixed https provider URLs
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
            response.read(65536)
            return 200 <= response.status < 300, ""
    except urllib.error.HTTPError as error:
        log.error("Email provider answered HTTP %s: %s", error.code, error.read(2048).decode("utf-8", "replace"))
        return False, "provider_error"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        log.error("Email provider unreachable: %s", error)
        return False, "provider_unreachable"


def build_mime(settings: MailSettings, message: Message, body_html: str) -> EmailMessage:
    """The MIME message SMTP delivers (plain text plus HTML alternative)."""
    email = EmailMessage()
    sender_name, sender = parseaddr(header_value(settings.sender, limit=MAX_ADDRESS * 2))
    email["From"] = formataddr((sender_name, sender)) if sender_name else sender
    email["To"] = message.to
    reply = _reply_to(settings)
    if reply[1]:
        email["Reply-To"] = formataddr(reply) if reply[0] else reply[1]
    email["Subject"] = message.subject
    if message.unsubscribe_url:
        email["List-Unsubscribe"] = f"<{header_value(message.unsubscribe_url, limit=2000)}>"
    email.set_content(message.text)
    email.add_alternative(body_html, subtype="html")
    return email


def _smtp(settings: MailSettings, message: Message, body_html: str) -> tuple[bool, str]:
    security = settings.smtp_security if settings.smtp_security in SMTP_SECURITY else "starttls"
    if settings.smtp_username and security == "none" and settings.smtp_host not in _LOCAL_HOSTS:
        return False, "smtp_requires_tls"
    email = build_mime(settings, message, body_html)
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.timeout,
                                                     context=context)
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.timeout)
        with client as smtp:
            if security == "starttls":
                smtp.starttls(context=context)
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(email)
        return True, ""
    except (smtplib.SMTPException, OSError) as error:
        log.error("SMTP delivery failed: %s", error)
        return False, "smtp_failed"


# ── HTML rendering ────────────────────────────────────────────────────────────


def _paragraphs(text: str) -> str:
    parts = []
    for paragraph in re.split(r"\n\s*\n", (text or "").strip()):
        if not paragraph:
            continue
        pieces, cursor = [], 0
        for match in _URL.finditer(paragraph):
            url = match.group(0).rstrip(".,);]")
            pieces.append(html.escape(paragraph[cursor:match.start()]))
            pieces.append(f'<a href="{html.escape(url, quote=True)}" style="color:#526bbf">{html.escape(url)}</a>')
            cursor = match.start() + len(url)
        pieces.append(html.escape(paragraph[cursor:]))
        body = "".join(pieces).replace("\n", "<br>")
        parts.append(f'<p style="margin:0 0 18px;color:#4b5063;font-size:16px;line-height:1.7">{body}</p>')
    return "".join(parts)


def _link(url: str) -> str:
    return html.escape(url, quote=True) if url.startswith(("https://", "http://")) else ""


def render_html(message: Message) -> str:
    """An email-client-safe HTML version (inline styles, tables, escaped text)."""
    title = html.escape(message.title or message.subject)
    brand = html.escape(message.brand)
    eyebrow = html.escape(message.eyebrow or message.brand)
    detail = ""
    if message.detail_value:
        detail = (
            '<table role="presentation" width="100%" style="margin:4px 0 22px;background:#f5f6fb;'
            'border:1px solid #e4e7f1;border-radius:8px"><tr><td style="padding:16px 18px">'
            f'<div style="color:#777d91;font-size:11px;font-weight:700;text-transform:uppercase">'
            f'{html.escape(message.detail_label)}</div>'
            f'<div style="color:#202534;font-size:18px;font-weight:700">{html.escape(message.detail_value)}</div>'
            "</td></tr></table>"
        )
    action = ""
    url = _link(message.action_url)
    if url and message.action_label:
        action = (
            '<table role="presentation" style="margin:4px 0 24px"><tr><td bgcolor="#7e9ada" style="border-radius:7px">'
            f'<a href="{url}" style="display:inline-block;padding:13px 22px;color:#111522;font-weight:700;'
            f'text-decoration:none">{html.escape(message.action_label)}</a></td></tr></table>'
            f'<p style="margin:0 0 20px;color:#858a9b;font-size:12px;word-break:break-all">{url}</p>'
        )
    footer = f'<p style="margin-top:20px;color:#8a8fa0;font-size:12px">{html.escape(message.footer)}</p>' \
        if message.footer else ""
    unsubscribe = _link(message.unsubscribe_url)
    if unsubscribe:
        footer += (f'<p style="margin:8px 0 0;font-size:12px"><a href="{unsubscribe}" style="color:#8a8fa0">'
                   f"{unsubscribe}</a></p>")
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{title}</title></head>"
        '<body style="margin:0;padding:0;background:#f1f2f6;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">'
        '<table role="presentation" width="100%" style="background:#f1f2f6"><tr><td align="center" style="padding:38px 16px">'
        '<table role="presentation" width="600" style="max-width:600px;width:100%">'
        f'<tr><td style="padding:0 8px 18px;color:#202534;font-size:18px;font-weight:750">{brand}</td></tr>'
        '<tr><td bgcolor="#ffffff" style="padding:40px 42px;border:1px solid #e4e7f1;border-radius:8px">'
        f'<div style="margin-bottom:11px;color:#777d91;font-size:11px;font-weight:700;text-transform:uppercase">{eyebrow}</div>'
        f'<h1 style="margin:0 0 20px;color:#202534;font-size:28px">{title}</h1>'
        f"{_paragraphs(message.text)}{detail}{action}{footer}"
        "</td></tr></table></td></tr></table></body></html>"
    )
