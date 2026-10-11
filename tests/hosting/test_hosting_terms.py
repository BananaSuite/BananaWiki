"""Asking existing accounts to accept new terms of service.

``HOSTING_TERMS_VERSION`` (by default the release's ``accounts.TERMS_VERSION``)
is the version new accounts accept on the sign-up form: the version date the
operator's published terms carry, never guessed by the portal. When the
operator publishes new terms, ``HOSTING_TERMS_REQUIRED_VERSION``
(off by default) sends every account that accepted an older version, or none,
to /account/terms before anything else in the portal. From there it can read
both documents, accept them, download its data, delete its account or sign
out.
"""

from __future__ import annotations

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting import accounts, instances
from bananawiki.hosting.config import load_config
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ

OLD = "2020-01-01"


@pytest.fixture
def strict(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(
        tmp_path, HOSTING_TERMS_REQUIRED_VERSION=accounts.TERMS_VERSION))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        accounts.create("root", PASSWORD, is_admin=True)
    return portal


def _scope(portal):
    return connection_scope(portal.extensions["bananawiki.hosting.database"])


def _sign_up(portal, username: str):
    client = portal.test_client()
    response = client.post("/signup", data={"username": username, "password": PASSWORD,
                                            "confirm_password": PASSWORD, "accept_terms": "1"})
    assert response.status_code == 302
    return client


def _row(portal, username: str) -> dict:
    with portal.app_context(), _scope(portal) as db:
        return db.one("SELECT * FROM accounts WHERE username = ?", (username,))


def _set(portal, username: str, **values) -> None:
    with portal.app_context(), _scope(portal) as db:
        db.update("accounts", values, "username = ?", (username,))


def _sign_in(portal, username: str):
    client = portal.test_client()
    assert client.post("/login", data={"username": username, "password": PASSWORD}).status_code == 302
    return client


def test_nothing_is_asked_while_the_setting_is_off(portal, make_account, login, query):
    account = make_account(terms_version=OLD, terms_accepted_at="2020-01-01 00:00:00")
    client = portal.test_client()
    login(client, account)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/account/terms").status_code == 302


def test_new_sign_ups_accept_the_current_version_and_are_not_asked_again(strict):
    client = _sign_up(strict, "fresh")
    assert _row(strict, "fresh")["terms_version"] == accounts.TERMS_VERSION
    assert client.get("/dashboard").status_code == 200


def test_an_account_that_accepted_older_terms_must_accept_the_new_ones(strict):
    client = _sign_up(strict, "veteran")
    _set(strict, "veteran", terms_version=OLD, terms_accepted_at="2020-01-01 00:00:00")
    for path in ("/dashboard", "/instances/create", "/account/api-tokens"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/account/terms"), path
    page = client.get("/account/terms").get_data(as_text=True)
    assert accounts.TERMS_VERSION in page and 'href="/terms"' in page and 'href="/privacy"' in page
    refused = client.post("/account/terms", data={})
    assert refused.status_code == 400 and _row(strict, "veteran")["terms_version"] == OLD
    accepted = client.post("/account/terms", data={"accept_terms": "1"})
    assert accepted.status_code == 302 and accepted.headers["Location"].endswith("/dashboard")
    row = _row(strict, "veteran")
    assert row["terms_version"] == accounts.TERMS_VERSION and row["terms_accepted_at"] > "2020-01-01 00:00:00"
    with strict.app_context(), _scope(strict) as db:
        event = db.one("SELECT actor_id, reason FROM hosting_events WHERE action = 'account.terms_accepted'")
    assert event == {"actor_id": row["id"], "reason": accounts.TERMS_VERSION}
    assert client.get("/dashboard").status_code == 200


def test_an_account_an_administrator_created_is_asked_too_without_becoming_a_new_sign_up(strict):
    with strict.app_context(), _scope(strict):
        accounts.create("handmade", PASSWORD)
    client = _sign_in(strict, "handmade")
    assert client.get("/dashboard").headers["Location"].endswith("/account/terms")
    client.post("/account/terms", data={"accept_terms": "1"})
    row = _row(strict, "handmade")
    assert row["terms_version"] == accounts.TERMS_VERSION
    assert row["terms_accepted_at"] is None, "it did not come from the sign-up form, so it is not a new account"
    with strict.test_request_context("/"), _scope(strict):
        assert not instances.on_probation(row)


def test_someone_who_does_not_accept_can_still_leave(strict):
    client = _sign_up(strict, "leaving")
    _set(strict, "leaving", terms_version=OLD)
    account_id = _row(strict, "leaving")["id"]
    assert client.get("/account").status_code == 200
    export = client.post("/account/export")
    assert export.status_code == 200 and export.headers["Content-Disposition"].startswith("attachment")
    assert client.post("/logout").status_code == 302
    client = _sign_in(strict, "leaving")
    deleted = client.post("/account/delete", data={"current_password": PASSWORD})
    assert deleted.status_code == 302 and deleted.headers["Location"].endswith("/login")
    with strict.app_context(), _scope(strict) as db:
        assert db.scalar("SELECT deleted_at FROM accounts WHERE id = ?", (account_id,))


def test_a_required_version_later_than_the_recorded_terms_refuses_to_start(tmp_path):
    """Asking for terms the portal cannot record would lock everyone out; it is a configuration error,
    not something to correct silently (accounts would never be asked to accept the new terms)."""
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, HOSTING_TERMS_REQUIRED_VERSION="2999-01-01"), secret_key="k" * 40)
    assert "HOSTING_TERMS_VERSION" in str(caught.value) and accounts.TERMS_VERSION in str(caught.value)


def test_the_operator_names_the_version_of_the_terms_it_publishes(tmp_path):
    published = "2026-10-09"
    portal = build_portal(tmp_path, environ=portal_environ(
        tmp_path, HOSTING_TERMS_VERSION=published, HOSTING_TERMS_REQUIRED_VERSION=published))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        accounts.create("root", PASSWORD, is_admin=True)
    client = _sign_up(portal, "newcomer")
    assert _row(portal, "newcomer")["terms_version"] == published
    assert client.get("/dashboard").status_code == 200
    _set(portal, "newcomer", terms_version=accounts.TERMS_VERSION)
    assert client.get("/dashboard").headers["Location"].endswith("/account/terms")
    assert published in client.get("/account/terms").get_data(as_text=True)
    client.post("/account/terms", data={"accept_terms": "1"})
    assert _row(portal, "newcomer")["terms_version"] == published
    assert client.get("/dashboard").status_code == 200


def test_without_a_version_the_release_s_own_is_recorded(tmp_path):
    cfg = load_config(portal_environ(tmp_path), secret_key="k" * 40)
    assert cfg.terms_version == accounts.TERMS_VERSION and cfg.terms_required_version == ""
    earlier = load_config(portal_environ(tmp_path, HOSTING_TERMS_VERSION="2026-01-01"), secret_key="k" * 40)
    assert earlier.terms_version == "2026-01-01", "an operator may still publish older terms"


def test_the_required_version_is_a_date(tmp_path):
    with pytest.raises(ConfigError):
        load_config(portal_environ(tmp_path, HOSTING_TERMS_REQUIRED_VERSION="soon"), secret_key="k" * 40)
    with pytest.raises(ConfigError):
        load_config(portal_environ(tmp_path, HOSTING_TERMS_VERSION="2026-10-9"), secret_key="k" * 40)
    assert load_config(portal_environ(tmp_path), secret_key="k" * 40).terms_required_version == ""


@pytest.mark.parametrize("name", ["HOSTING_TERMS_VERSION", "HOSTING_TERMS_REQUIRED_VERSION"])
@pytest.mark.parametrize("value", ["2026-13-01", "2026-02-30", "0000-00-00"])
def test_a_version_must_be_a_real_date(tmp_path, name, value):
    """Versions are compared as text: a date that cannot exist would order wrongly against real ones."""
    with pytest.raises(ConfigError) as caught:
        load_config(portal_environ(tmp_path, **{name: value}), secret_key="k" * 40)
    assert name in str(caught.value)


def test_the_terms_page_is_translated(strict):
    client = _sign_up(strict, "italiano")
    _set(strict, "italiano", terms_version=OLD)
    client.post("/language", data={"language": "it"})
    page = client.get("/account/terms").get_data(as_text=True)
    assert "Accetto" in page and "termini di servizio" in page


OWN_PAGES = {"HOSTING_TERMS_URL": "https://legal.example.org/terms",
             "HOSTING_PRIVACY_URL": "https://legal.example.org/privacy"}


def test_own_legal_pages_without_their_version_are_reported(tmp_path, caplog):
    """Accounts would be recorded as accepting the release's default version, not the operator's pages."""
    with caplog.at_level("WARNING", logger="bananawiki.hosting.config"):
        cfg = load_config(portal_environ(tmp_path, **OWN_PAGES), secret_key="k" * 40)
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("HOSTING_TERMS_VERSION" in text and accounts.TERMS_VERSION in text for text in warnings), warnings
    assert cfg.terms_version == accounts.TERMS_VERSION and cfg.terms_required_version == ""


@pytest.mark.parametrize("extra", [{}, {**OWN_PAGES, "HOSTING_TERMS_VERSION": "2026-10-09"}])
def test_nothing_is_reported_when_the_version_is_known(tmp_path, caplog, extra):
    with caplog.at_level("WARNING", logger="bananawiki.hosting.config"):
        load_config(portal_environ(tmp_path, **extra), secret_key="k" * 40)
    assert not [record for record in caplog.records if "HOSTING_TERMS_VERSION" in record.getMessage()]


def test_own_legal_pages_do_not_ask_anyone_to_accept_again(tmp_path):
    """Pointing the links at the operator's pages changes no version: accounts that accepted the terms
    keep using the portal until the operator requires a new version."""
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, **OWN_PAGES))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        accounts.create("root", PASSWORD, is_admin=True)
    _sign_up(portal, "settled")
    _set(portal, "settled", terms_version=OLD)
    assert _sign_in(portal, "settled").get("/dashboard").status_code == 200
