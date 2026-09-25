"""The legacy redirect daemon must be told which domains it is moving between.

Earlier versions defaulted to the domains of one particular deployment, so an
operator who installed the service without editing the unit file sent their
visitors to somebody else's site. Both settings are now required, and the
module refuses to import without them.

Each test imports the module fresh, because the domains are read once at
import time.
"""

import importlib
import sys

import pytest

VARS = ("LEGACY_OLD_DOMAINS", "LEGACY_OLD_DOMAIN", "LEGACY_NEW_DOMAIN", "LEGACY_NEW_SCHEME")


def load(monkeypatch, **settings):
    """Import legacy_redirect.app with exactly the given settings."""
    for name in VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    sys.modules.pop("legacy_redirect.app", None)
    return importlib.import_module("legacy_redirect.app")


def test_import_fails_without_the_old_domains(monkeypatch):
    with pytest.raises(RuntimeError, match="LEGACY_OLD_DOMAINS"):
        load(monkeypatch, LEGACY_NEW_DOMAIN="wiki.example.net")


def test_import_fails_without_the_new_domain(monkeypatch):
    with pytest.raises(RuntimeError, match="LEGACY_NEW_DOMAIN"):
        load(monkeypatch, LEGACY_OLD_DOMAINS="old.example.com")


def test_several_old_domains_are_matched_and_normalized(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS=" Old.Example.COM , legacy.example.org ",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    assert app.OLD_DOMAINS == ["old.example.com", "legacy.example.org"]
    assert app.NEW_DOMAIN == "wiki.example.net"


def test_the_singular_variable_still_works_and_does_not_duplicate(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS="old.example.com",
        LEGACY_OLD_DOMAIN="old.example.com",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    assert app.OLD_DOMAINS == ["old.example.com"]


def test_a_subdomain_carries_over_to_the_new_domain(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS="old.example.com",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    assert app._extract_subdomain("team.old.example.com") == ("team", "old.example.com")
    assert app._extract_subdomain("team.old.example.com:8090") == ("team", "old.example.com")


def test_the_apex_and_www_have_no_subdomain_to_carry(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS="old.example.com",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    assert app._extract_subdomain("old.example.com") == (None, None)
    assert app._extract_subdomain("www.old.example.com") == (None, None)


def test_an_unrelated_host_is_not_redirected(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS="old.example.com",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    # A domain that merely ends in the same letters must not match.
    assert app._extract_subdomain("team.notold.example.com") == (None, None)
    assert app._extract_subdomain("team.example.net") == (None, None)


def test_a_hostile_subdomain_is_escaped_in_the_page(monkeypatch):
    app = load(
        monkeypatch,
        LEGACY_OLD_DOMAINS="old.example.com",
        LEGACY_NEW_DOMAIN="wiki.example.net",
    )
    page = app._build_page('"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
