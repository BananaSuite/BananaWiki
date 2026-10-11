"""The portal's legal links lead to the operator's pages, or the static site's, in the visitor's language."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting import accounts
from bananawiki.hosting.config import load_config
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ

HOSTING = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting"
TEMPLATES, CONTENT = HOSTING / "templates", HOSTING / "content"
# How templates and help articles may link to the legal pages: through the portal's own redirects.
ALLOWED_LEGAL_LINKS = {"{{ url_for('public.terms') }}", "{{ url_for('public.privacy') }}", "url_for('public.terms')",
                       "url_for('public.privacy')", "url_for('account.terms')", "/terms", "/privacy"}

ITALIAN_PAGES = {"/terms": "https://wiki.test/terms/index.it.html",
                 "/privacy": "https://wiki.test/privacy/index.it.html",
                 "/compliance": "https://wiki.test/terms/index.it.html"}
ENGLISH_PAGES = {"/terms": "https://wiki.test/terms/", "/privacy": "https://wiki.test/privacy/",
                 "/compliance": "https://wiki.test/terms/"}


@pytest.mark.parametrize("path", sorted(ITALIAN_PAGES))
def test_italian_visitors_get_the_italian_legal_pages(web, path):
    response = web.get(path, headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.5"})
    assert response.status_code == 302 and response.headers["Location"] == ITALIAN_PAGES[path]


@pytest.mark.parametrize("path", sorted(ENGLISH_PAGES))
def test_everyone_else_gets_the_english_ones(web, path):
    assert web.get(path).headers["Location"] == ENGLISH_PAGES[path]
    assert web.get(path, headers={"Accept-Language": "de"}).headers["Location"] == ENGLISH_PAGES[path]


def test_the_chosen_interface_language_wins_over_the_browser(web):
    web.post("/language", data={"language": "it"})
    assert web.get("/terms", headers={"Accept-Language": "en"}).headers["Location"] == ITALIAN_PAGES["/terms"]
    web.post("/language", data={"language": "en"})
    assert web.get("/privacy", headers={"Accept-Language": "it"}).headers["Location"] == ENGLISH_PAGES["/privacy"]


def test_without_a_base_domain_the_links_fall_back_to_the_help_centre(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, BASE_DOMAIN=""))
    response = portal.test_client().get("/terms", headers={"Accept-Language": "it"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/help")


# ── An operator's own legal pages (HOSTING_TERMS_URL, HOSTING_PRIVACY_URL) ───

OWN = {"HOSTING_TERMS_URL": "https://legal.example.org/terms", "HOSTING_PRIVACY_URL": "https://legal.example.org/privacy"}
OWN_PAGES = {"/terms": OWN["HOSTING_TERMS_URL"], "/privacy": OWN["HOSTING_PRIVACY_URL"],
             "/compliance": OWN["HOSTING_TERMS_URL"]}


def _portal(tmp_path, **extra):
    return build_portal(tmp_path, environ=portal_environ(tmp_path, **extra))


@pytest.mark.parametrize("language", ["en", "it", "de"])
def test_the_operator_s_own_pages_replace_the_static_site_s(tmp_path, language):
    client = _portal(tmp_path, **OWN).test_client()
    for path, target in OWN_PAGES.items():
        response = client.get(path, headers={"Accept-Language": language})
        assert response.status_code == 302 and response.headers["Location"] == target, path


def test_an_italian_variant_is_given_to_italian_visitors_only(tmp_path):
    client = _portal(tmp_path, **OWN, HOSTING_TERMS_URL_IT="https://legal.example.org/it/termini",
                     HOSTING_PRIVACY_URL_IT="https://legal.example.org/it/privacy").test_client()
    italian = {path: client.get(path, headers={"Accept-Language": "it"}).headers["Location"] for path in OWN_PAGES}
    assert italian == {"/terms": "https://legal.example.org/it/termini",
                       "/privacy": "https://legal.example.org/it/privacy",
                       "/compliance": "https://legal.example.org/it/termini"}
    assert {path: client.get(path).headers["Location"] for path in OWN_PAGES} == OWN_PAGES


def test_one_italian_variant_leaves_the_other_page_on_its_english_address(tmp_path):
    client = _portal(tmp_path, **OWN, HOSTING_PRIVACY_URL_IT="https://legal.example.org/it/privacy").test_client()
    assert client.get("/terms", headers={"Accept-Language": "it"}).headers["Location"] == OWN["HOSTING_TERMS_URL"]
    assert client.get("/privacy", headers={"Accept-Language": "it"}).headers["Location"] == \
        "https://legal.example.org/it/privacy"


def test_the_operator_s_pages_need_no_base_domain(tmp_path):
    client = _portal(tmp_path, **OWN, BASE_DOMAIN="").test_client()
    assert client.get("/privacy").headers["Location"] == OWN["HOSTING_PRIVACY_URL"]


@pytest.mark.parametrize("value", [
    "http://legal.example.org/terms", "https://user:secret@legal.example.org/terms", "/terms", "legal.example.org/terms",
    "https://", "javascript:alert(1)", "https://legal.example.org/te rms", "https://[legal.example.org/terms",
    # Ports that the redirect could not encode: a configuration error, not a failing /terms page.
    "https://legal.example.org:99999/terms", "https://legal.example.org:abc/terms",
    # Plain HTTP only for an onion address, not for a name that merely contains one.
    "http://legal.onion.example.org/terms", "http://legal.example.org/terms.onion",
])
def test_an_address_must_be_a_plain_https_url(tmp_path, value):
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, **{**OWN, "HOSTING_TERMS_URL": value}), secret_key="k" * 40)
    assert "HOSTING_TERMS_URL" in str(caught.value)


def test_an_onion_site_may_link_its_own_pages_over_plain_http(tmp_path):
    """Tor already encrypts the connection, and onion sites usually have no certificate."""
    onion = "http://" + "a" * 56 + ".onion"
    client = _portal(tmp_path, HOSTING_TERMS_URL=f"{onion}/terms", HOSTING_PRIVACY_URL=f"{onion}/privacy",
                     HOSTING_TERMS_URL_IT=f"{onion}:8080/it/termini").test_client()
    assert client.get("/privacy").headers["Location"] == f"{onion}/privacy"
    assert client.get("/terms", headers={"Accept-Language": "it"}).headers["Location"] == f"{onion}:8080/it/termini"


def test_both_pages_are_set_together(tmp_path):
    """Half the links on the operator's site and half on a static site they may not run would mislead."""
    for name in OWN:
        with pytest.raises(ConfigError) as caught:
            load_config(portal_environ(tmp_path, **{name: OWN[name]}), secret_key="k" * 40)
        assert "HOSTING_TERMS_URL" in str(caught.value) and "HOSTING_PRIVACY_URL" in str(caught.value)


def test_a_translation_needs_the_page_itself(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, HOSTING_TERMS_URL_IT="https://legal.example.org/it/termini"),
                    secret_key="k" * 40)
    assert "HOSTING_TERMS_URL_IT" in str(caught.value) and "HOSTING_TERMS_URL" in str(caught.value)


def test_every_legal_link_of_the_portal_goes_through_its_own_redirect(tmp_path):
    """The sign-up form, the new-terms page, the wiki forms and the help centre all link to /terms and
    /privacy, so the operator's addresses (or the static site's) apply everywhere at once."""
    legal = re.compile(r"terms|privacy|compliance")
    found = set()
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        hrefs = re.findall(r'href="([^"]*)"', text) + re.findall(r"link\((url_for\('[a-z_.]+'\))", text)
        for href in filter(legal.search, hrefs):
            found.add(href)
            assert href in ALLOWED_LEGAL_LINKS, (path.name, href)
    for path in sorted(CONTENT.glob("help.*.json")):
        for article in json.loads(path.read_text(encoding="utf-8")):
            for section in article.get("sections", []):
                for href in filter(legal.search, re.findall(r'href="([^"]*)"', section.get("html", ""))):
                    found.add(href)
                    assert href in ALLOWED_LEGAL_LINKS, (path.name, article["slug"], href)
    assert {"url_for('public.terms')", "url_for('public.privacy')", "/privacy"} <= found
    portal = _portal(tmp_path, **OWN)
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        accounts.create("root", PASSWORD, is_admin=True)  # past the first (administrator) sign-up
    signup = portal.test_client().get("/signup").get_data(as_text=True)
    assert 'href="/terms"' in signup and 'href="/privacy"' in signup and "legal.example.org" not in signup
