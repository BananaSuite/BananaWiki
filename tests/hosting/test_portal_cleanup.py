"""Portal clean-up: one place for each control, labels that say what happens, no dead ends."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.hosting import email

from .hosting_support import PASSWORD, build_portal, portal_environ

HOSTING = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting"
REASON = "Our documentation should be open to everyone."


def _client(portal, login, account):
    client = portal.test_client()
    login(client, account)
    return client


@pytest.fixture
def admin(make_account):
    return make_account("chief", admin=True, email="chief@example.org")


@pytest.fixture
def outbox():
    with email.capture_outbox() as messages:
        yield messages


# Feature requests answered from the wiki's admin page


def _requested(portal, login, make_account, make_wiki):
    owner = make_account(email="owner@example.org")
    wiki = make_wiki(owner, "docs")
    owner_client = _client(portal, login, owner)
    owner_client.post(f"/instances/{wiki['id']}/features/public_access/requests", data={"reason": REASON})
    return owner, wiki, owner_client


def test_the_wiki_admin_page_answers_a_waiting_request_instead_of_offering_allow(
        portal, make_account, make_wiki, login, admin, query):
    _owner, wiki, _ = _requested(portal, login, make_account, make_wiki)
    request = query("SELECT id FROM instance_feature_requests", one=True)
    page = _client(portal, login, admin).get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert f"/admin/feature-requests/{request['id']}/approve" in page
    assert f"/admin/feature-requests/{request['id']}/deny" in page
    assert "/features/public_access/entitlement" not in page, "no Allow next to the request"
    assert "/features/page_builder/entitlement" in page, "a feature nobody asked for keeps its Allow"


def test_allowing_a_requested_feature_approves_the_request(portal, make_account, make_wiki, login, admin, query):
    owner, wiki, owner_client = _requested(portal, login, make_account, make_wiki)
    admin_client = _client(portal, login, admin)
    assert 'data-attention-count="1"' in admin_client.get("/admin").get_data(as_text=True)
    admin_client.post(f"/admin/instances/{wiki['id']}/features/public_access/entitlement", data={"allowed": "1"})
    request = query("SELECT status, reviewed_by FROM instance_feature_requests", one=True)
    assert request["status"] == "approved" and request["reviewed_by"] == admin["id"]
    assert query("SELECT public_wiki_allowed FROM instances WHERE id = ?", (wiki["id"],), one=True)[
        "public_wiki_allowed"] == 1
    notice = query("SELECT outcome FROM hosting_account_notices WHERE account_id = ?", (owner["id"],), one=True)
    assert notice["outcome"] == "approved", "the owner hears about it"
    assert "data-attention-count" not in admin_client.get("/admin").get_data(as_text=True)
    page = owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "Cancel request" not in page


# The owner view and Admin tools


def test_an_administrator_runs_someone_elses_wiki_from_admin_tools(make_account, make_wiki, login, admin, portal):
    owner = make_account()
    wiki = make_wiki(owner)
    page = _client(portal, login, admin).get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "You are seeing this wiki as its owner sees it" in page
    assert f'href="/admin/instances/{wiki["id"]}"' in page
    assert f"/instances/{wiki['id']}/terminate" not in page and f"/instances/{wiki['id']}/stop" not in page
    assert "Wiki controls" not in page
    own = _client(portal, login, owner).get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "Wiki controls" in own and f"/instances/{wiki['id']}/terminate" in own
    assert "You are seeing this wiki" not in own


def test_terminating_from_the_owner_view_as_an_administrator_counts_as_theirs(
        make_account, make_wiki, login, admin, portal, query, outbox):
    owner = make_account(email="owner@example.org")
    wiki = make_wiki(owner, "gone")
    response = _client(portal, login, admin).post(f"/instances/{wiki['id']}/terminate")
    assert response.headers["Location"].endswith(f"/admin/instances/{wiki['id']}")
    row = query("SELECT status, terminated_reason FROM instances WHERE id = ?", (wiki["id"],), one=True)
    assert row["status"] == "terminated" and row["terminated_reason"] == "admin"
    assert [message.to for message in outbox] == ["owner@example.org"]


def test_an_owner_terminating_their_own_wiki_is_not_told_about_it(make_account, make_wiki, login, portal, query,
                                                                    outbox):
    owner = make_account(email="owner@example.org")
    wiki = make_wiki(owner)
    _client(portal, login, owner).post(f"/instances/{wiki['id']}/terminate")
    assert query("SELECT terminated_reason FROM instances WHERE id = ?", (wiki["id"],), one=True)[
        "terminated_reason"] == "manual"
    assert outbox == []


def test_admin_tools_restart_and_switch_the_mode(make_account, make_wiki, login, admin, portal, query):
    wiki = make_wiki(make_account())
    client = _client(portal, login, admin)
    page = client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "Force restart" not in page and ">Restart</button>" in page
    assert "Switch to easy mode (BM mode)" in page and "Administration → BM mode" in page
    response = client.post(f"/admin/instances/{wiki['id']}/toggle-easy-wiki", data={"enable": "1"})
    assert response.status_code == 302
    assert query("SELECT easy_wiki FROM instances WHERE id = ?", (wiki["id"],), one=True)["easy_wiki"] == 1
    assert "Switch to full mode" in client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)


def test_never_expiring_reads_never_on_the_admin_page(make_account, make_wiki, login, admin, portal):
    wiki = make_wiki(make_account())
    client = _client(portal, login, admin)
    client.post(f"/admin/instances/{wiki['id']}/make-indefinite")
    page = client.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    expires = re.search(r"<dt>Expires</dt><dd>(.*?)</dd>", page)
    assert expires and expires.group(1).startswith("Never "), "the stored date stays visible, small, for the record"


# Transfers and collaborator permissions


def test_an_administrator_moves_a_wiki_from_admin_tools_not_with_an_offer(make_account, make_wiki, login, admin,
                                                                           portal):
    owner = make_account()
    wiki = make_wiki(owner)
    page = _client(portal, login, admin).get(f"/instances/{wiki['id']}/collaborators").get_data(as_text=True)
    assert f"/instances/{wiki['id']}/transfer" not in page
    assert "Only the owner can offer this wiki to someone" in page
    tools = _client(portal, login, admin).get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    assert "Move now" in tools and "without asking them to accept" in tools
    own = _client(portal, login, owner).get(f"/instances/{wiki['id']}/collaborators").get_data(as_text=True)
    assert f"/instances/{wiki['id']}/transfer" in own and "Only the owner can offer" not in own


def test_the_download_permission_is_not_offered(make_account, make_wiki, login, portal):
    owner = make_account()
    wiki = make_wiki(owner)
    page = _client(portal, login, owner).get(f"/instances/{wiki['id']}/collaborators").get_data(as_text=True)
    assert 'value="start_stop"' in page and 'value="download"' not in page


def test_collaborators_who_may_start_a_wiki_see_start_in_their_list(make_account, make_wiki, login, portal, query):
    owner, starter, viewer = make_account(), make_account(), make_account()
    wiki = make_wiki(owner)
    owner_client = _client(portal, login, owner)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": starter["username"], "role": "custom", "permissions": ["start_stop"]})
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": viewer["username"], "role": "custom", "permissions": ["view"]})
    owner_client.post(f"/instances/{wiki['id']}/stop")
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "stopped"
    start = f"/instances/{wiki['id']}/restart"
    assert start in owner_client.get("/dashboard").get_data(as_text=True)
    assert start in _client(portal, login, starter).get("/dashboard").get_data(as_text=True)
    assert start not in _client(portal, login, viewer).get("/dashboard").get_data(as_text=True)


# Easy mode is the wiki's BM mode


def test_easy_mode_is_named_after_bm_mode(make_account, make_wiki, login, portal):
    owner = make_account()
    client = _client(portal, login, owner)
    create = client.get("/instances/create").get_data(as_text=True)
    assert "Easy mode (BM mode)" in create and "Administration → BM mode" in create
    wiki = make_wiki(owner)
    page = client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "Starting mode" in page and "Switch to easy mode (BM mode)" in page


# What "BM" stands for is never written, here included: only the SHA-256 of the lower-case word is.
LONG_NAME_SHA256 = "65f25fb53caae60144b24e4072f0e7719e175e8ab8fc04f85bb60b129874f774"


def test_the_long_name_of_bm_mode_never_appears():
    import hashlib
    import re

    pieces = set()  # every run of 11 letters, also inside longer words
    for path in HOSTING.parent.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".html", ".py", ".js", ".md", ".css"):
            for word in re.findall(r"[a-z]{11,}", path.read_text(encoding="utf-8", errors="replace").lower()):
                pieces.update(word[start:start + 11] for start in range(len(word) - 10))
    assert LONG_NAME_SHA256 not in {hashlib.sha256(piece.encode()).hexdigest() for piece in pieces}


# Needs your attention: one list, on the Overview


def test_the_attention_page_leads_to_the_overview(make_account, login, admin, portal, query):
    pending = make_account(approval_status="pending")
    query("UPDATE accounts SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (pending["id"],))
    client = _client(portal, login, admin)
    response = client.get("/admin/attention")
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin#attention")
    overview = client.get("/admin").get_data(as_text=True)
    assert 'id="attention"' in overview and "Oldest request arrived" in overview
    assert 'href="/admin/attention"' not in overview, "no second page, no extra tab"


def test_the_sign_in_banner_reviews_on_the_overview(make_account, login, admin, portal):
    make_account(approval_status="pending")
    page = _client(portal, login, admin).get("/admin").get_data(as_text=True)
    assert 'href="/admin#attention"' in page


# Impersonation leaves the customer's theme alone


def test_the_theme_toggle_while_impersonating_is_the_administrators(make_account, login, admin, portal, query):
    customer = make_account(theme_mode="light")
    client = _client(portal, login, admin)
    client.post(f"/admin/accounts/{customer['id']}/impersonate")
    client.post("/account/theme", data={"theme_mode": "dark"})
    assert query("SELECT theme_mode FROM accounts WHERE id = ?", (customer["id"],), one=True)["theme_mode"] == "light"
    assert query("SELECT theme_mode FROM accounts WHERE id = ?", (admin["id"],), one=True)["theme_mode"] == "dark"
    assert 'data-theme="dark"' in client.get("/dashboard").get_data(as_text=True)


# Texts


def test_a_wiki_without_a_custom_domain_is_told_whom_to_ask(tmp_path, make_account):
    portal = build_portal(tmp_path / "domains", environ=portal_environ(
        tmp_path / "domains", HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test", HOSTING_CONTACT_EMAIL="help@wiki.test"))
    from bananawiki.hosting import accounts, instances
    from bananawiki.hosting.db import connection_scope

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        owner = accounts.create("domainless", PASSWORD)
        wiki = instances.create(owner, "plain")[0]
    client = portal.test_client()
    client.post("/login", data={"username": "domainless", "password": PASSWORD})
    page = client.get(f"/instances/{wiki['id']}/domain").get_data(as_text=True)
    assert "cannot use a custom domain yet" in page and "mailto:help@wiki.test" in page
    assert f"/instances/{wiki['id']}/features/custom_domain/requests" in page, "or ask right there"


def test_help_says_where_start_is():
    for lang, words in (("en", "a stopped wiki shows a <strong>Start</strong> button at the top of its page"),
                        ("it", "mostra il pulsante <strong>Avvia</strong> in cima alla sua pagina"),
                        ("de", "zeigt oben auf seiner Seite")):
        articles = json.loads((HOSTING / "content" / f"help.{lang}.json").read_text(encoding="utf-8"))
        article = next(article for article in articles if article["slug"] == "account-management")
        assert any(words in section["html"] for section in article["sections"])
