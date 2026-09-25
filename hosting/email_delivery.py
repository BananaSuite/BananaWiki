"""Small transactional-email adapter for Brevo, Resend, and SMTP.

Includes a daily send counter to stay within free-tier quotas.
"""


from __future__ import annotations

from http_transport import open_http

import html as html_lib
import json
import logging
import re
import smtplib
import threading
import urllib.error
import urllib.request
from datetime import date
from email.message import EmailMessage
from email.utils import parseaddr

from . import config

_logger = logging.getLogger("hosting.email")

_URL_RE = re.compile(r"https?://[^\s<>]+")


def _linkify_email_text(value):
    """Escape user-controlled copy and make plain web addresses clickable."""
    parts = []
    cursor = 0
    for match in _URL_RE.finditer(value):
        parts.append(html_lib.escape(value[cursor:match.start()]))
        url = match.group(0)
        clean_url = url.rstrip(".,);]")
        suffix = url[len(clean_url):]
        safe_url = html_lib.escape(clean_url, quote=True)
        parts.append(
            '<a href="{}" style="color:#526bbf;text-decoration:underline;">{}</a>'.format(
                safe_url, safe_url
            )
        )
        parts.append(html_lib.escape(suffix))
        cursor = match.end()
    parts.append(html_lib.escape(value[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def _email_copy_html(text):
    paragraphs = re.split(r"\n\s*\n", (text or "").strip())
    return "".join(
        '<p style="margin:0 0 18px;color:#4b5063;font-size:16px;line-height:1.7;">{}</p>'.format(
            _linkify_email_text(paragraph)
        )
        for paragraph in paragraphs
        if paragraph
    )


def render_email_html(
    *, title, text, eyebrow="BANANAWIKI HOSTING", action_url=None,
    action_label=None, detail_label=None, detail_value=None,
):
    """Render a responsive, email-client-safe transactional email shell."""
    safe_title = html_lib.escape(title or "BananaWiki update")
    safe_eyebrow = html_lib.escape(eyebrow or "BANANAWIKI HOSTING")
    preview = " ".join((text or title or "BananaWiki account update").split())[:150]
    safe_preview = html_lib.escape(preview)
    copy_html = _email_copy_html(text)
    safe_contact = html_lib.escape(config.HOSTING_CONTACT_EMAIL)

    # The footer links to this operator's site and, if one is configured,
    # its status page, never to one particular deployment.
    link_style = 'style="color:#747b91;text-decoration:none;"'
    footer_parts = []
    if config.HOSTING_MODE == "subdomain" and config.BASE_DOMAIN:
        footer_parts.append('<a href="https://%s" %s>BananaWiki</a>' % (
            html_lib.escape(config.BASE_DOMAIN), link_style))
    if config.HOSTING_STATUS_URL:
        footer_parts.append('<a href="%s" %s>Service status</a>' % (
            html_lib.escape(config.HOSTING_STATUS_URL), link_style))
    footer_links = '<span style="padding:0 7px;color:#c2c5ce;">&bull;</span>'.join(footer_parts)

    detail_html = ""
    if detail_value:
        detail_html = """
          <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="margin:4px 0 22px;background:#f5f6fb;border:1px solid #e4e7f1;border-radius:8px;">
            <tr><td style="padding:16px 18px;">
              <div style="color:#777d91;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">%s</div>
              <div style="color:#202534;font-size:18px;font-weight:700;word-break:break-word;">%s</div>
            </td></tr>
          </table>""" % (
            html_lib.escape(detail_label or "DETAIL"),
            html_lib.escape(str(detail_value)),
        )

    action_html = ""
    if (
        isinstance(action_url, str)
        and action_url.startswith(("https://", "http://"))
        and action_label
    ):
        safe_action_url = html_lib.escape(action_url, quote=True)
        safe_action_label = html_lib.escape(action_label)
        action_html = """
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 24px;">
            <tr><td bgcolor="#7e9ada" style="border-radius:7px;">
              <a href="%s" style="display:inline-block;padding:13px 22px;color:#111522;font-size:15px;font-weight:700;line-height:1;text-decoration:none;border-radius:7px;">%s</a>
            </td></tr>
          </table>
          <p style="margin:0 0 20px;color:#858a9b;font-size:12px;line-height:1.6;word-break:break-all;">
            If the button does not work, paste this link into your browser:<br>
            <a href="%s" style="color:#526bbf;text-decoration:underline;">%s</a>
          </p>""" % (safe_action_url, safe_action_label, safe_action_url, safe_action_url)

    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title>%s</title>
  <style>
    @media only screen and (max-width:620px) {
      .email-shell { width:100%% !important; }
      .email-card { padding:30px 22px !important; }
      .email-header { padding:0 8px 18px !important; }
      .email-footer { padding:22px 12px 0 !important; }
      h1 { font-size:26px !important; }
    }
  </style>
</head>
<body style="margin:0;padding:0;background:#f1f2f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">%s</div>
  <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#f1f2f6;">
    <tr><td align="center" style="padding:38px 16px;">
      <table role="presentation" class="email-shell" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;">
        <tr><td class="email-header" style="padding:0 8px 18px;">
          <div style="color:#202534;font-size:18px;font-weight:750;letter-spacing:-.2px;">BananaWiki <span style="color:#7e8498;font-size:12px;font-weight:650;letter-spacing:.7px;">HOSTING</span></div>
        </td></tr>
        <tr><td class="email-card" bgcolor="#ffffff" style="padding:42px 46px 38px;background:#ffffff;border:1px solid #e4e7f1;border-radius:8px;">
          <div style="margin-bottom:11px;color:#777d91;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;">%s</div>
          <h1 style="margin:0 0 20px;color:#202534;font-size:30px;line-height:1.2;letter-spacing:-.7px;font-weight:750;">%s</h1>
          %s
          %s
          %s
          <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid #eceef4;">
            <tr><td style="padding-top:20px;color:#8a8fa0;font-size:12px;line-height:1.6;">
              This automated message was sent for your BananaWiki Hosting account. Need help? Reply to this email or contact <a href="mailto:%s" style="color:#626f9e;text-decoration:underline;">%s</a>.
            </td></tr>
          </table>
        </td></tr>
        <tr><td class="email-footer" align="center" style="padding:22px 12px 0;color:#9296a5;font-size:12px;line-height:1.7;">
          <div style="margin-bottom:5px;">Built for knowledge that belongs to you.</div>
          %s
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""" % (
        safe_title, safe_preview, safe_eyebrow, safe_title, copy_html,
        detail_html, action_html, safe_contact, safe_contact, footer_links,
    )

# The send counter lives in this process only: it starts from zero on every
# restart, and each worker keeps its own tally.
_counter_lock = threading.Lock()
_daily_count = 0
_counter_date = date.today()

# Default daily limit.  Override via HOSTING_EMAIL_DAILY_LIMIT env var.
# 0 = unlimited.  Brevo free = 300, Resend free = 100.
DAILY_LIMIT = int(getattr(config, "HOSTING_EMAIL_DAILY_LIMIT", 0) or 0)


def _check_and_increment():
    """Return True if we are within the daily limit, and increment the counter."""
    global _daily_count, _counter_date
    if DAILY_LIMIT <= 0:
        return True  # no limit configured
    with _counter_lock:
        today = date.today()
        if today != _counter_date:
            _daily_count = 0
            _counter_date = today
        if _daily_count >= DAILY_LIMIT:
            return False
        _daily_count += 1
        return True


def get_daily_email_stats():
    """Return (sent_today, daily_limit) for admin dashboard display."""
    with _counter_lock:
        today = date.today()
        count = _daily_count if today == _counter_date else 0
    return count, DAILY_LIMIT


_recipient_lock = threading.Lock()
_recipient_counts = {}  # {email: (date, count)}

# Max emails to a single address per day.  Prevents a rogue user from
# hammering "resend verification" or "forgot password" to exhaust the
# global daily quota.
PER_RECIPIENT_DAILY_LIMIT = 15


def _check_recipient_limit(to):
    """Return True if the recipient hasn't exceeded their daily limit."""
    with _recipient_lock:
        today = date.today()
        entry = _recipient_counts.get(to)
        if not entry or entry[0] != today:
            _recipient_counts[to] = (today, 1)
            return True
        if entry[1] >= PER_RECIPIENT_DAILY_LIMIT:
            return False
        _recipient_counts[to] = (today, entry[1] + 1)
        return True


def is_configured():
    provider = config.HOSTING_EMAIL_PROVIDER
    if provider in ("brevo", "resend"):
        return bool(config.HOSTING_EMAIL_API_KEY and parseaddr(config.HOSTING_EMAIL_FROM)[1])
    if provider == "smtp":
        return bool(config.HOSTING_EMAIL_SMTP_HOST and parseaddr(config.HOSTING_EMAIL_FROM)[1])
    return False


def _api_post(url, headers, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with open_http(req, timeout=15, public_only=False) as response:
            response.read(65536)
            return 200 <= response.status < 300, ""
    except urllib.error.HTTPError as exc:
        # Read the error body for logging (helps diagnose Brevo/Resend
        # rejections) but keep credentials out of user-visible messages.
        detail = ""
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
            _logger.error("Email API error HTTP %d: %s", exc.code, body)
            # Try to extract a human-readable message from JSON responses
            try:
                detail = json.loads(body).get("message", "")
            except Exception:
                pass
        except Exception:
            pass
        msg = f"provider returned HTTP {exc.code}"
        if detail:
            msg += f" ({detail})"
        return False, msg
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _logger.error("Email delivery network error: %s", exc)
        return False, "provider could not be reached"


def send_email(*, to, subject, text, html=None):
    """Send one transactional message. Returns ``(ok, safe_error)``."""
    if not is_configured():
        return False, "transactional email is not configured"
    if not _check_recipient_limit(to):
        _logger.warning("Per-recipient limit reached for %s (%d/day)", to, PER_RECIPIENT_DAILY_LIMIT)
        return False, "too many emails to this address today, try again tomorrow"
    if not _check_and_increment():
        _logger.warning("Daily email limit reached (%d). Skipping email to %s", DAILY_LIMIT, to)
        return False, "daily email limit reached"
    if not html:
        html = render_email_html(title=subject, text=text)
    sender_name, sender_email = parseaddr(config.HOSTING_EMAIL_FROM)
    reply_name, reply_email = parseaddr(config.HOSTING_EMAIL_REPLY_TO)
    # Fall back to the raw value if parseaddr can't extract an email
    # (e.g. a bare "user@example.com" without angle brackets).
    if not reply_email:
        reply_email = config.HOSTING_EMAIL_REPLY_TO.strip()
    provider = config.HOSTING_EMAIL_PROVIDER
    if provider == "brevo":
        reply_to = {"email": reply_email}
        if reply_name:
            reply_to["name"] = reply_name
        payload = {
            "sender": {"name": sender_name or "BananaWiki", "email": sender_email},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": text,
            "replyTo": reply_to,
        }
        # Only include htmlContent when there is actual HTML to send;
        # Brevo rejects an empty-string htmlContent with HTTP 400.
        if html:
            payload["htmlContent"] = html
        return _api_post(
            "https://api.brevo.com/v3/smtp/email",
            {"api-key": config.HOSTING_EMAIL_API_KEY},
            payload,
        )
    if provider == "resend":
        payload = {
            "from": config.HOSTING_EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "text": text,
            "reply_to": reply_email,
        }
        # Only include html when provided; Resend accepts text-only emails.
        if html:
            payload["html"] = html
        return _api_post(
            "https://api.resend.com/emails",
            {"Authorization": f"Bearer {config.HOSTING_EMAIL_API_KEY}"},
            payload,
        )

    message = EmailMessage()
    message["From"] = config.HOSTING_EMAIL_FROM
    message["To"] = to
    message["Reply-To"] = config.HOSTING_EMAIL_REPLY_TO
    message["Subject"] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(
            config.HOSTING_EMAIL_SMTP_HOST,
            config.HOSTING_EMAIL_SMTP_PORT,
            timeout=15,
        ) as smtp:
            if config.HOSTING_EMAIL_SMTP_TLS:
                smtp.starttls()
            if config.HOSTING_EMAIL_SMTP_USERNAME:
                if not config.HOSTING_EMAIL_SMTP_TLS and config.HOSTING_EMAIL_SMTP_HOST not in ("localhost", "127.0.0.1", "::1"):
                    # Sending the SMTP password in the clear to a remote relay
                    # is never what an operator meant by turning TLS off.
                    return False, "SMTP credentials require STARTTLS on a remote host"
                smtp.login(
                    config.HOSTING_EMAIL_SMTP_USERNAME,
                    config.HOSTING_EMAIL_SMTP_PASSWORD,
                )
            smtp.send_message(message)
        return True, ""
    except (smtplib.SMTPException, OSError):
        return False, "SMTP delivery failed"
