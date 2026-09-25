"""The hosting platform tells people to contact the operator that runs it.

Its pages, emails and help centre used to carry one deployment's address and
portal hostname, so every other operator's users were sent to that
deployment. These tests pin each place to the configured contact address and
portal, and check that nothing falls back to the old hard-coded values.
"""

import json
from pathlib import Path

import pytest

from hosting import config as hosting_config
from hosting import email_delivery, help_content, notifications
from hosting._subdomain_proxy import _proxy_error_response

OPERATOR = "help@example.org"
OLD = ("contact@bananawiki.com", "hosting.bananawiki.com", "status.bananawiki.com")


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_CONTACT_EMAIL", OPERATOR)
    monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "example.org")
    monkeypatch.setattr(hosting_config, "EFFECTIVE_PORTAL_DOMAIN", "portal.example.org")
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", "subdomain")
    monkeypatch.setattr(hosting_config, "HOSTING_STATUS_URL", "")


def _assert_clean(text):
    for value in OLD:
        assert value not in text, value


# Where the address comes from

def test_an_explicit_contact_address_wins(monkeypatch):
    monkeypatch.setenv("HOSTING_CONTACT_EMAIL", "ops@example.net")
    monkeypatch.setattr(hosting_config, "HOSTING_EMAIL_REPLY_TO", "Support <reply@example.net>")
    assert hosting_config._contact_email() == "ops@example.net"


def test_the_reply_to_address_is_the_next_choice(monkeypatch):
    monkeypatch.delenv("HOSTING_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(hosting_config, "HOSTING_EMAIL_REPLY_TO", "Support <reply@example.net>")
    assert hosting_config._contact_email() == "reply@example.net"


def test_otherwise_contact_at_the_base_domain(monkeypatch):
    monkeypatch.delenv("HOSTING_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(hosting_config, "HOSTING_EMAIL_REPLY_TO", "")
    monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "example.net")
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", "subdomain")
    assert hosting_config._contact_email() == "contact@example.net"


@pytest.mark.parametrize("base_domain", ["", "localhost", "203.0.113.10", "2001:db8::1"])
def test_without_a_domain_the_placeholder_is_used(monkeypatch, base_domain):
    # contact@203.0.113.10 is not an address anyone can write to. These values
    # must be detected as port mode on their own, not because a test says so.
    monkeypatch.delenv("HOSTING_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("HOSTING_MODE", raising=False)
    monkeypatch.setattr(hosting_config, "HOSTING_EMAIL_REPLY_TO", "")
    monkeypatch.setattr(hosting_config, "BASE_DOMAIN", base_domain)
    mode = hosting_config._detect_hosting_mode()
    assert mode == "port"
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", mode)
    assert hosting_config._contact_email() == hosting_config.CONTACT_EMAIL_PLACEHOLDER


def test_a_domain_in_port_mode_still_gets_the_placeholder(monkeypatch):
    # An operator can force port mode on a real domain; the portal is then not
    # at that domain, so guessing contact@ it would be wrong.
    monkeypatch.delenv("HOSTING_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(hosting_config, "HOSTING_EMAIL_REPLY_TO", "")
    monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "example.net")
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", "port")
    assert hosting_config._contact_email() == hosting_config.CONTACT_EMAIL_PLACEHOLDER


@pytest.mark.parametrize("value", ['x"><script>@a.b', "not-an-address", "a b@c.d"])
def test_a_malformed_contact_address_is_refused(monkeypatch, value):
    monkeypatch.setenv("HOSTING_CONTACT_EMAIL", value)
    with pytest.raises(ValueError):
        hosting_config._contact_email()


@pytest.mark.parametrize("url", [
    "https://:secret@status.example.org",
    "https://user:secret@status.example.org",
    "https://@status.example.org",
    "ftp://status.example.org",
    "status.example.org",
])
def test_a_status_url_with_credentials_or_no_scheme_is_refused(url):
    with pytest.raises(ValueError):
        hosting_config._status_url(url)


def test_a_plain_status_url_is_kept():
    assert hosting_config._status_url(" https://status.example.org/ ") == "https://status.example.org/"
    assert hosting_config._status_url("") == ""


# Emails

def test_every_notification_names_the_operator_and_the_person(operator, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "_safe_send", lambda **kw: sent.append(kw) or True)
    account = {"email": "user@example.com", "username": "maria"}

    notifications.notify_account_suspended(account, reason="r")
    notifications.notify_account_unsuspended(account)
    notifications.notify_account_pending_deletion(account)
    notifications.notify_account_deleted("user@example.com", "maria")
    notifications.notify_account_approved(account)
    notifications.notify_account_denied(account)
    notifications.notify_instance_created(account, "notes")
    notifications.notify_instance_terminated(account, "notes")
    notifications.notify_instance_suspended(account, "notes")

    assert len(sent) == 9
    for message in sent:
        text = message["text"]
        _assert_clean(text)
        # The username used to go missing when the address was spliced into
        # a string that was then formatted piecemeal.
        assert "maria" in text and "{}" not in text
    contact_messages = [m for m in sent if "contact" in m["text"].lower()]
    assert contact_messages and all(OPERATOR in m["text"] for m in contact_messages)
    assert any("https://portal.example.org" in m["text"] for m in sent)


def test_the_email_footer_links_to_this_operator(operator, monkeypatch):
    html = email_delivery.render_email_html(title="Hello", text="Body")
    _assert_clean(html)
    assert f"mailto:{OPERATOR}" in html
    assert 'href="https://example.org"' in html
    assert "Service status" not in html

    monkeypatch.setattr(hosting_config, "HOSTING_STATUS_URL", "https://status.example.org")
    html = email_delivery.render_email_html(title="Hello", text="Body")
    assert 'href="https://status.example.org"' in html


def test_port_mode_emails_do_not_link_to_the_bare_base_domain(operator, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", "port")
    monkeypatch.setattr(hosting_config, "BASE_DOMAIN", "203.0.113.10")
    html = email_delivery.render_email_html(title="Hello", text="Body")
    assert 'href="https://203.0.113.10"' not in html


@pytest.mark.parametrize("port, expected", [
    (5099, "https://[2001:db8::1]:5099/dashboard"),
    (443, "https://[2001:db8::1]/dashboard"),
])
def test_port_mode_portal_links_bracket_ipv6(monkeypatch, port, expected):
    monkeypatch.setattr(hosting_config, "HOSTING_MODE", "port")
    monkeypatch.setattr(hosting_config, "HOSTING_PUBLIC_HOST", "2001:db8::1")
    monkeypatch.setattr(hosting_config, "HOSTING_PUBLIC_SCHEME", "https")
    monkeypatch.setattr(hosting_config, "HOSTING_PORT", port)
    assert notifications._portal_url("/dashboard") == expected


# Pages

def test_the_suspended_wiki_page_names_the_operator(operator):
    status, headers, body = _proxy_error_response("suspended", "notes")
    page = b"".join(body).decode() if isinstance(body, (list, tuple)) else str(body)
    _assert_clean(page)
    assert OPERATOR in page


def test_no_shipped_text_carries_the_old_address():
    root = Path(__file__).resolve().parents[1]
    for path in list((root / "hosting" / "templates").rglob("*.html")) + list(
        (root / "hosting" / "routes").glob("*.py")
    ) + [
        root / "hosting" / "content" / "help.en.json",
        root / "hosting" / "content" / "help.it.json",
        root / "hosting" / "notifications.py",
        root / "hosting" / "email_delivery.py",
        root / "hosting" / "_subdomain_proxy.py",
    ]:
        _assert_clean(path.read_text(encoding="utf-8"))
    for lang in ("en", "it"):
        pack = json.loads((root / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        for key, value in pack.items():
            if key.startswith("hosting."):
                _assert_clean(value)


# Help centre

def test_the_help_centre_shows_the_configured_address(operator):
    for language in help_content.LANGUAGES:
        article = help_content.fill_placeholders(
            help_content.get_article(language, "contact"), hosting_config.HOSTING_CONTACT_EMAIL)
        html = "".join(section["html"] for section in article["sections"])
        assert f"mailto:{OPERATOR}" in html
        assert "{contact_email}" not in html


def test_the_help_centre_links_to_this_deployments_source(operator):
    source = "https://git.example.org/ops/bananawiki"
    for language in help_content.LANGUAGES:
        for raw in help_content.load_articles(language):
            article = help_content.fill_placeholders(raw, OPERATOR, source)
            html = "".join(section["html"] for section in article["sections"])
            assert "{source_url}" not in html and "{contact_email}" not in html, (language, raw["slug"])
        article = help_content.fill_placeholders(
            help_content.get_article(language, "move-to-your-own-server"), OPERATOR, source)
        assert f'href="{source}"' in "".join(section["html"] for section in article["sections"])


def test_the_new_articles_exist_in_both_languages():
    for slug in ("features", "move-to-your-own-server"):
        for language in help_content.LANGUAGES:
            assert help_content.get_article(language, slug) is not None, (slug, language)


def test_every_translation_with_an_email_placeholder_gets_one():
    """A key whose text says {email} has to be given the address wherever it is
    used, in a template or in Python, or the page shows the placeholder
    itself."""
    import re
    root = Path(__file__).resolve().parents[1]
    pack = json.loads((root / "translations" / "en.json").read_text(encoding="utf-8"))
    keys = {key for key, value in pack.items() if "{email}" in value}
    assert keys
    call = re.compile(r"\b_?t\(\s*(['\"])([^'\"]+)\1([^)]*)\)")
    sources = list((root / "hosting").rglob("*.html")) + list((root / "hosting").rglob("*.py"))
    checked = 0
    for source in sources:
        for _quote, key, arguments in call.findall(source.read_text(encoding="utf-8")):
            if key in keys:
                checked += 1
                assert "email=" in arguments, (source.name, key)
    assert checked, "no call site found for any {email} key"


def test_the_expiry_notice_has_no_space_before_its_comma():
    from jinja2 import Template

    root = Path(__file__).resolve().parents[1]
    source = (root / "hosting" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    start = source.index("{% for inst in expiring_soon %}")
    end = source.index("to request an extension.") + len("to request an extension.")
    text = Template(source[start:end]).render(
        expiring_soon=[{"subdomain": "a", "days_remaining": 3}, {"subdomain": "b", "days_remaining": 1}],
        contact_email=OPERATOR,
    )
    text = " ".join(text.split())
    assert "a (3d remaining), b (1d remaining), email" in text
    assert " ," not in text
