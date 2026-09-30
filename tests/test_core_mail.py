"""Shared outgoing email (bananawiki.core.mail): validation, providers, quotas and rendering."""

from __future__ import annotations

import json

import pytest

from bananawiki.core import mail
from bananawiki.core.env import ConfigError, Env

SMTP = mail.MailSettings(provider="smtp", sender="Wiki <wiki@example.org>", smtp_host="smtp.example.org",
                         smtp_port=587, smtp_username="wiki", smtp_password="pw")
MESSAGE = mail.Message(to="Admin@Example.ORG", subject="Hello", text="Body with a link https://wiki.example.org/x.",
                       title="Title", action_url="https://wiki.example.org/attention", action_label="Open",
                       unsubscribe_url="https://wiki.example.org/attention/unsubscribe/abc", brand="My <Wiki>")


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, message):
        self.sent = message


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


# ── Validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("address,ok", [
    ("a@example.org", True), ("first.last+tag@sub.example.co", True), ("no-at-sign", False),
    ("a@b", False), ("a@example.org\r\nBcc: x@example.org", False), ("Name <a@example.org>", False),
    ("a" * 250 + "@x.io", False), ("", False),
])
def test_valid_address(address, ok):
    assert mail.valid_address(address) is ok


def test_normalize_lowercases_the_domain_only():
    assert mail.normalize_address(" Bob@Example.ORG ") == "Bob@example.org"
    with pytest.raises(mail.MailError):
        mail.normalize_address("bad")


def test_header_injection_is_refused():
    with pytest.raises(mail.MailError):
        mail.header_value("Hi\r\nBcc: victim@example.org")
    ok, reason = mail.send(SMTP, mail.Message(to="a@example.org", subject="x\nBcc: y@example.org", text="t"))
    assert (ok, reason) == (False, "invalid_header")


def test_is_configured():
    assert mail.is_configured(SMTP)
    assert not mail.is_configured(None)
    assert not mail.is_configured(mail.MailSettings(provider="smtp", sender="wiki@example.org"))
    assert not mail.is_configured(mail.MailSettings(provider="brevo", sender="wiki@example.org"))
    assert mail.is_configured(mail.MailSettings(provider="resend", sender="wiki@example.org", api_key="k"))
    assert not mail.is_configured(mail.MailSettings(provider="smtp", sender="bad", smtp_host="h"))


def test_secrets_are_not_in_repr():
    assert "pw" not in repr(SMTP) and "pw" not in repr(mail.MailSettings(api_key="pw"))


# ── Environment ───────────────────────────────────────────────────────────────


def test_settings_from_env():
    assert mail.settings_from_env(Env({}), "BW_") is None
    settings = mail.settings_from_env(Env({"BW_SMTP_HOST": "mail.example.org", "BW_SMTP_PORT": "465",
                                           "BW_MAIL_FROM": "Wiki <wiki@example.org>", "BW_SMTP_PASSWORD": "s"}), "BW_")
    assert settings.provider == "smtp" and settings.smtp_security == "ssl" and settings.smtp_password == "s"
    api = mail.settings_from_env(Env({"BW_MAIL_PROVIDER": "resend", "BW_MAIL_API_KEY": "k",
                                      "BW_MAIL_FROM": "wiki@example.org"}), "BW_")
    assert api.provider == "resend" and mail.is_configured(api)


@pytest.mark.parametrize("environ", [
    {"BW_MAIL_PROVIDER": "carrier-pigeon"},
    {"BW_SMTP_HOST": "h", "BW_SMTP_SECURITY": "maybe"},
    {"BW_SMTP_HOST": "h", "BW_MAIL_FROM": "not an address"},
    {"BW_SMTP_HOST": "h", "BW_SMTP_PORT": "99999"},
])
def test_settings_from_env_refuses_bad_values(environ):
    with pytest.raises(ConfigError):
        mail.settings_from_env(Env(environ), "BW_")


# ── SMTP ──────────────────────────────────────────────────────────────────────


def test_smtp_starttls(smtp):
    assert mail.send(SMTP, MESSAGE) == (True, "")
    client = smtp.instances[0]
    assert (client.host, client.port, client.timeout) == ("smtp.example.org", 587, mail.DEFAULT_TIMEOUT)
    assert client.calls == ["starttls", "login:wiki"]
    sent = client.sent
    assert sent["To"] == "Admin@example.org" and sent["Subject"] == "Hello"
    assert sent["From"] == "Wiki <wiki@example.org>"
    assert sent["List-Unsubscribe"] == "<https://wiki.example.org/attention/unsubscribe/abc>"
    assert sent.get_body(("plain",)).get_content().startswith("Body with a link")


def test_smtp_implicit_tls(smtp):
    settings = mail.MailSettings(**{**SMTP.__dict__, "smtp_security": "ssl", "smtp_port": 465})
    assert mail.send(settings, MESSAGE) == (True, "")
    assert smtp.instances[0].context is not None and smtp.instances[0].calls == ["login:wiki"]


def test_smtp_refuses_plain_passwords_to_remote_servers(smtp):
    settings = mail.MailSettings(**{**SMTP.__dict__, "smtp_security": "none"})
    assert mail.send(settings, MESSAGE) == (False, "smtp_requires_tls")
    assert smtp.instances == []
    local = mail.MailSettings(**{**SMTP.__dict__, "smtp_security": "none", "smtp_host": "localhost"})
    assert mail.send(local, MESSAGE) == (True, "")


def test_smtp_failure_is_a_reason_not_an_exception(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(mail.smtplib, "SMTP", refuse)
    assert mail.send(SMTP, MESSAGE) == (False, "smtp_failed")


def test_not_configured_sends_nothing(smtp):
    assert mail.send(mail.MailSettings(), MESSAGE) == (False, "not_configured")
    assert smtp.instances == []


# ── HTTP providers ────────────────────────────────────────────────────────────


class FakeResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return b"{}"


@pytest.fixture
def http(monkeypatch):
    requests = []

    def urlopen(request, timeout=None):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(mail.urllib.request, "urlopen", urlopen)
    return requests


def test_brevo_payload(http):
    settings = mail.MailSettings(provider="brevo", sender="Wiki <wiki@example.org>", api_key="key-1",
                                 reply_to="help@example.org", timeout=7)
    assert mail.send(settings, MESSAGE) == (True, "")
    request, timeout = http[0]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.brevo.com/v3/smtp/email" and timeout == 7
    assert request.get_header("Api-key") == "key-1"
    assert payload["sender"] == {"name": "Wiki", "email": "wiki@example.org"}
    assert payload["to"] == [{"email": "Admin@example.org"}] and payload["replyTo"] == {"email": "help@example.org"}
    assert payload["headers"]["List-Unsubscribe"] == "<https://wiki.example.org/attention/unsubscribe/abc>"


def test_resend_payload(http):
    settings = mail.MailSettings(provider="resend", sender="wiki@example.org", api_key="key-2")
    assert mail.send(settings, MESSAGE) == (True, "")
    request, _timeout = http[0]
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_header("Authorization") == "Bearer key-2"
    assert json.loads(request.data)["to"] == ["Admin@example.org"]


def test_provider_errors(monkeypatch):
    import urllib.error

    def fail(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(mail.urllib.request, "urlopen", fail)
    settings = mail.MailSettings(provider="resend", sender="wiki@example.org", api_key="k")
    assert mail.send(settings, MESSAGE) == (False, "provider_unreachable")


# ── Quotas, outbox, HTML ─────────────────────────────────────────────────────


def test_daily_limiter():
    limiter = mail.DailyLimiter(per_recipient=2)
    assert limiter.allow("A@x.org", 3) is None and limiter.allow("a@x.org", 3) is None
    assert limiter.allow("a@x.org", 3) == "recipient_limit"
    assert limiter.allow("b@x.org", 3) is None
    assert limiter.allow("c@x.org", 3) == "daily_limit"
    assert limiter.sent_today() == 3


def test_capture_outbox_collects_instead_of_sending(smtp):
    with mail.capture_outbox() as outbox:
        assert mail.send(None, MESSAGE) == (True, "")
    assert outbox[0].to == "Admin@example.org" and smtp.instances == []


def test_html_is_escaped_and_links_are_safe():
    html = mail.render_html(mail.Message(to="a@example.org", subject="<b>s</b>", text="<script>x</script>",
                                         action_url="javascript:alert(1)", action_label="Go", brand="<Wiki>"))
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;Wiki&gt;" in html
    assert "javascript:" not in html
    rendered = mail.render_html(MESSAGE)
    assert 'href="https://wiki.example.org/attention"' in rendered
    assert "https://wiki.example.org/attention/unsubscribe/abc" in rendered
