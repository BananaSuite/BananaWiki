"""The name the platform goes by (``HOSTING_SERVICE_NAME``, default "BananaWiki Hosting").

An operator running the portal for their own organisation names it once: the
page titles and the top bar, the emails (subject, text, heading and footer),
the label authenticator apps show for a new enrolment, the maintenance page
and a wiki's status page all use it. What describes the software itself (the
footer's version line, command-line help and log lines) keeps its own name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting import accounts, email, mfa, notifications
from bananawiki.hosting.app import _MaintenanceGate
from bananawiki.hosting.config import DEFAULT_SERVICE_NAME, load_config
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.i18n import LANGUAGES

from .hosting_support import PASSWORD, build_portal, portal_environ

NAME = 'Acme & "Co" <Wikis>'
ESCAPED = "Acme &amp; &#34;Co&#34; &lt;Wikis&gt;"
TRANSLATIONS = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting" / "translations"


def _portal(tmp_path, name: str | None = NAME, **extra):
    environ = portal_environ(tmp_path, **extra)
    if name is not None:
        environ["HOSTING_SERVICE_NAME"] = name
    return build_portal(tmp_path, environ=environ)


def _scope(portal):
    return connection_scope(portal.extensions["bananawiki.hosting.database"])


def _brand(page: str) -> tuple[str, str]:
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    topbar = re.search(r'class="topbar__brand".*?<span>(.*?)</span>', page, re.S).group(1)
    return title, topbar


def test_the_default_name_is_bananawiki_hosting(tmp_path, web):
    assert DEFAULT_SERVICE_NAME == "BananaWiki Hosting"
    assert load_config(portal_environ(tmp_path), secret_key="k" * 40).service_name == DEFAULT_SERVICE_NAME
    assert load_config(portal_environ(tmp_path, HOSTING_SERVICE_NAME="  "), secret_key="k" * 40).service_name == \
        DEFAULT_SERVICE_NAME
    title, topbar = _brand(web.get("/login").get_data(as_text=True))
    assert title.endswith("· BananaWiki Hosting") and topbar == "BananaWiki Hosting"


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_pages_carry_the_operator_s_name_escaped(tmp_path, language):
    client = _portal(tmp_path).test_client()
    client.post("/language", data={"language": language})
    for path in ("/login", "/signup", "/help"):
        page = client.get(path).get_data(as_text=True)
        title, topbar = _brand(page)
        assert title.endswith("· " + ESCAPED) and topbar == ESCAPED, path
        assert NAME not in page, "the name is always escaped"
        assert "{service}" not in page


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_emails_carry_the_operator_s_name(tmp_path, language):
    portal = _portal(tmp_path)
    with portal.app_context(), _scope(portal) as db:
        accounts.create("mailed", PASSWORD, email="mailed@example.org")
        db.execute("UPDATE accounts SET email_verified_at = '2026-01-01 00:00:00' WHERE username = 'mailed'")
    client = portal.test_client()
    client.post("/language", data={"language": language})
    with email.capture_outbox() as outbox:
        client.post("/forgot-username", data={"email": "mailed@example.org"})
    message = outbox[0]
    assert NAME in message.subject and NAME in message.footer
    assert message.brand == NAME and message.eyebrow == NAME
    for text in (message.subject, message.text, message.title, message.footer, message.brand, message.eyebrow):
        assert "BananaWiki Hosting" not in text and "{service}" not in text


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_emails_in_a_chosen_language_carry_the_operator_s_name(tmp_path, language):
    """Emails sent in the recipient's own language rather than the request's (administrators' digests,
    decisions, expiry notices) fill the name in too: the body and the footer name it."""
    portal = _portal(tmp_path)
    with portal.test_request_context("/"), _scope(portal), email.capture_outbox() as outbox:
        ok, _reason = notifications.send("admin@example.org", "admin_attention", username="admin", lang=language,
                                         count=1, lines="- 1", manage="", detail_value="1",
                                         action_url="https://wiki.test/admin/attention")
    assert ok, _reason
    message = outbox[0]
    assert NAME in message.text and NAME in message.footer
    for text in (message.subject, message.text, message.title, message.footer):
        assert "BananaWiki Hosting" not in text and "{service}" not in text


def test_authenticator_apps_show_the_name_for_new_enrolments(tmp_path):
    portal = _portal(tmp_path)
    with portal.app_context():
        uri = mfa.provisioning_uri("JBSWY3DPEHPK3PXP", "alice")
    assert uri.startswith("otpauth://totp/Acme%20%26%20%22Co%22%20%3CWikis%3E%3Aalice?")
    assert "issuer=Acme%20%26%20%22Co%22%20%3CWikis%3E&" in uri


def test_renaming_the_platform_keeps_enrolled_authenticators_working(tmp_path):
    """The issuer is only the label of the entry in the app: codes depend on the secret alone."""
    before = _portal(tmp_path, name=None)
    secret = mfa.generate_secret()
    with before.app_context(), _scope(before):
        account = accounts.create("enrolled", PASSWORD)
        assert mfa.enable(account["id"], secret, -1)
    after = _portal(tmp_path, name="Example Wikis")
    with after.app_context(), _scope(after):
        assert mfa.consume_totp(account["id"], mfa.totp_code(secret))


def test_the_maintenance_page_carries_the_name(tmp_path):
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__, NAME)
    for language in sorted(LANGUAGES):
        body = b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", "HTTP_ACCEPT_LANGUAGE": language},
                             lambda status, headers: None)).decode()
        assert body.count("Acme &amp; &quot;Co&quot; &lt;Wikis&gt;") == 2 and "BananaWiki Hosting" not in body
    portal = _portal(tmp_path, BANANA_MAINTENANCE_FILE=__file__)
    page = portal.test_client().get("/login", headers={"Accept": "text/html"}).get_data(as_text=True)
    assert "Acme &amp; &quot;Co&quot; &lt;Wikis&gt;" in page


def test_a_wiki_s_status_page_carries_the_name(tmp_path):
    page = _portal(tmp_path).test_client().get("/", headers={"Host": "nobody-hosting.wiki.test"})
    assert page.status_code == 404 and ESCAPED in page.get_data(as_text=True)


def test_a_wiki_s_status_page_puts_the_portal_link_on_its_own_line(tmp_path):
    """The portal link sits on its own line under the name, so a wrapped line never starts with the
    separator dot."""
    page = _portal(tmp_path).test_client().get("/", headers={"Host": "nobody-hosting.wiki.test"}).get_data(as_text=True)
    footer = re.search(r"<footer[^>]*>(.*?)</footer>", page, re.S).group(1)
    line = re.search(r'<p><span class="brand">(.*?)</p>', footer, re.S).group(1)
    assert "·" not in line and re.search(r"</span><br>\s*<a href=", line)


def test_the_footer_still_names_the_software_and_its_version(tmp_path):
    page = _portal(tmp_path).test_client().get("/login").get_data(as_text=True)
    footer = re.search(r'<footer class="site-footer">(.*?)</footer>', page, re.S).group(1)
    assert re.search(r"BananaWiki Hosting \d+\.\d+", footer)


def test_no_message_hard_codes_the_name():
    """Every sentence that names the platform uses {service}; only the footer's version line names the software."""
    for language in sorted(LANGUAGES):
        strings = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
        named = sorted(key for key, value in strings.items() if "BananaWiki Hosting" in value)
        assert named == ["hosting.footer"], (language, named)


@pytest.mark.parametrize("value", ["x" * 61, "Acme\nWikis", "Acme\tWikis", "Acme‮Wikis", "Acme {username}",
                                   "Acme }"])
def test_the_name_is_a_short_single_line_without_placeholders(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, HOSTING_SERVICE_NAME=value), secret_key="k" * 40)
    assert "HOSTING_SERVICE_NAME" in str(caught.value)


@pytest.mark.parametrize("value", ["x" * 60, "Wiki della Scuola", "Anna's: wikis", "Ünïcødé Wikis 🍌"])
def test_ordinary_names_are_accepted(tmp_path, value):
    assert load_config(portal_environ(tmp_path, HOSTING_SERVICE_NAME=value), secret_key="k" * 40).service_name == value
