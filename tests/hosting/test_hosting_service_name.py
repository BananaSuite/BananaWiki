"""The name the platform goes by (``HOSTING_SERVICE_NAME``, default "BananaWiki Hosting").

An operator running the portal for their own organisation names it once: the
page titles and the top bar, the emails (subject, text, heading and footer),
the label authenticator apps show for a new enrolment, the maintenance page
and a wiki's status page all use it. What describes the software itself (the
footer's version line, command-line help and log lines) keeps its own name.
"""

from __future__ import annotations

import re

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting import accounts, mfa
from bananawiki.hosting.config import load_config
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.i18n import LANGUAGES

from .hosting_support import PASSWORD, build_portal, portal_environ

NAME = 'Acme & "Co" <Wikis>'
ESCAPED = "Acme &amp; &#34;Co&#34; &lt;Wikis&gt;"
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


@pytest.mark.parametrize("value", ["x" * 61, "Acme\nWikis", "Acme\tWikis", "Acme‮Wikis", "Acme {username}",
                                   "Acme }"])
def test_the_name_is_a_short_single_line_without_placeholders(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, HOSTING_SERVICE_NAME=value), secret_key="k" * 40)
    assert "HOSTING_SERVICE_NAME" in str(caught.value)


@pytest.mark.parametrize("value", ["x" * 60, "Wiki della Scuola", "Anna's: wikis", "Ünïcødé Wikis 🍌"])
def test_ordinary_names_are_accepted(tmp_path, value):
    assert load_config(portal_environ(tmp_path, HOSTING_SERVICE_NAME=value), secret_key="k" * 40).service_name == value
