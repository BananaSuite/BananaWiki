"""Caddy's on-demand TLS "ask" endpoint: never limited on loopback, and it says what it refused."""

from __future__ import annotations

import logging

import pytest

from bananawiki.hosting.app import GLOBAL_RATE_LIMIT
from bananawiki.hosting.routes import public

ASK = "/internal/domains/authorize"
LOG = "bananawiki.hosting.public"


@pytest.fixture(autouse=True)
def fresh_denials():
    public._denials.reset()


def test_scanner_noise_cannot_make_caddy_refuse_a_real_wiki(web, make_account, make_wiki):
    make_wiki(make_account(), "genuine")
    for number in range(GLOBAL_RATE_LIMIT + 120):
        assert web.get(f"{ASK}?domain=guess{number}.wiki.test").status_code == 403
    assert web.get(f"{ASK}?domain=genuine-hosting.wiki.test").status_code == 200
    assert web.get(f"{ASK}?domain=genuine-hosting.wiki.test").headers["Cache-Control"] == "no-store"


def test_the_portal_pages_on_loopback_stay_limited(web):
    for _ in range(GLOBAL_RATE_LIMIT):
        web.get(ASK + "?domain=a.wiki.test")
    assert web.get(ASK + "?domain=a.wiki.test").status_code == 403
    codes = {web.get("/login").status_code for _ in range(GLOBAL_RATE_LIMIT + 5)}
    assert 429 in codes


def test_other_addresses_still_get_404_and_the_limit(web):
    remote = {"environ_overrides": {"REMOTE_ADDR": "203.0.113.9"}}
    statuses = {web.get(ASK + "?domain=a.wiki.test", **remote).status_code for _ in range(GLOBAL_RATE_LIMIT + 5)}
    assert statuses == {404, 429}


def test_a_refused_name_is_logged_once_a_minute(web, caplog):
    with caplog.at_level(logging.INFO, logger=LOG):
        for _ in range(3):
            web.get(ASK + "?domain=nobody.wiki.test")
        web.get(ASK + "?domain=someone.wiki.test")
    assert [record.getMessage() for record in caplog.records] == [
        "Certificate refused for 'nobody.wiki.test'", "Certificate refused for 'someone.wiki.test'"]


def test_an_allowed_name_is_not_logged(web, make_account, make_wiki, caplog):
    make_wiki(make_account(), "genuine")
    with caplog.at_level(logging.INFO, logger=LOG):
        assert web.get(ASK + "?domain=genuine-hosting.wiki.test").status_code == 200
    assert caplog.records == []


def test_a_logged_name_is_one_short_line(web, caplog):
    with caplog.at_level(logging.INFO, logger=LOG):
        web.get(ASK + "?domain=" + "evil%0AWARNING%20forged%1B%5B31m" + "x" * 500)
    (record,) = caplog.records
    message = record.getMessage()
    assert "\n" not in message and "\x1b" not in message
    assert message.startswith("Certificate refused for 'evil\\nWARNING forged\\x1b[31mxxx")
    assert len(message) < 160
