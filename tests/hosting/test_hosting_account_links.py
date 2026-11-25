"""Portal admin → a wiki → Account links (SIGNIN-SPEC.md A4 and B): the wiki's accounts with their links, one
link, bulk links with a preview (every conflict type, a stale preview refused), unlinking, the hosting-only
switch refused while an administrator is unlinked, the notice email, and the wiki side through the runtime
agent (the real tenant task)."""

from __future__ import annotations

import html
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from bananawiki.hosting.runtime import WikiUser

from .agent_fakes import make_runtime, make_spec, provision

DAY = "2026-01-01 00:00:00"


@pytest.fixture
def site(portal, web, make_account, make_wiki, login, runtime, query):
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    admin = make_account("chief", admin=True)
    owner = make_account("owner")
    wiki = make_wiki(owner, "team")
    tenant = runtime.tenants[wiki["subdomain"]]
    login(web, admin)
    return {"admin": admin, "owner": owner, "wiki": wiki, "tenant": tenant}


def _wiki_user(tenant, name: str, role: str = "user", *, email: str = "", state: str = "active",
               password: bool = True) -> None:
    tenant.users[name] = WikiUser(name, role, DAY)
    if email:
        tenant.emails[name] = email
    if state != "active":
        tenant.states[name] = state
    if not password:
        tenant.passwordless.add(name)


def _url(site, path: str = "") -> str:
    return f"/admin/instances/{site['wiki']['id']}/account-links{path}"


def _preview(web, site, **form):
    response = web.post(_url(site, "/preview"), data=form)
    page = response.get_data(as_text=True)
    fields = {}
    if "/account-links/apply" in page:  # the Link form of the preview: what it sends back
        form = page[page.index("/account-links/apply"):]
        form = form[:form.index("</form>")]
        fields = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)">', form))
        fields = {key: html.unescape(value) for key, value in fields.items()}
    return response, page, fields


def _row(page: str, name: str) -> str:
    start = page.index(f"<td data-label=\"Wiki account\">{name}")
    return page[start:page.index("</tr>", start)]


def _portal_link(query, site, account, wiki_user_id: str) -> None:
    query("INSERT INTO hosting_oauth_account_links (instance_id, account_id, wiki_user_id, wiki_username) "
          "VALUES (?, ?, ?, ?)", (site["wiki"]["id"], account["id"], wiki_user_id, wiki_user_id))


def test_the_page_lists_the_wiki_accounts_and_their_links(web, site, make_account, query):
    alice = make_account("alice")
    _wiki_user(site["tenant"], "alice", email="alice@example.org")
    _wiki_user(site["tenant"], "ghost", password=False)
    _portal_link(query, site, alice, "alice")
    page = web.get(_url(site)).get_data(as_text=True)
    assert "Account links" in page and "alice@example.org" in page and "No password" in page
    assert "Only on the portal" in page, "a link the wiki does not have yet shows as such"
    assert f"/admin/instances/{site['wiki']['id']}/account-links" in web.get(
        f"/admin/instances/{site['wiki']['id']}").get_data(as_text=True)


def test_bulk_preview_shows_every_case(web, site, make_account, query):
    tenant = site["tenant"]
    make_account("ann")
    _wiki_user(tenant, "ann")                                     # will link
    _wiki_user(tenant, "nobody")                                  # not found
    make_account("boss")
    _wiki_user(tenant, "boss", "admin")                           # skipped: administrator
    make_account("sleepy")
    _wiki_user(tenant, "sleepy", state="suspended")               # skipped: disabled wiki account
    taken = make_account("taken")
    _wiki_user(tenant, "taken")
    _portal_link(query, site, taken, "someone-else")              # conflict: hosting account linked elsewhere
    other = make_account("other")
    make_account("moved")
    _wiki_user(tenant, "moved")
    tenant.links["moved"] = {"account_id": other["id"], "portal_username": "other", "linked_at": DAY}  # conflict
    make_account("frozen", suspended=1)
    _wiki_user(tenant, "frozen")                                  # conflict: suspended hosting account
    make_account("waiting", approval_status="pending")
    _wiki_user(tenant, "waiting")                                 # conflict: pending hosting account
    done = make_account("done")
    _wiki_user(tenant, "done")
    tenant.links["done"] = {"account_id": done["id"], "portal_username": "done", "linked_at": DAY}  # already linked
    response, page, fields = _preview(web, site, mode="username")
    assert response.status_code == 200
    expectations = {
        "ann": "Will link", "nobody": "Not found", "boss": "Administrator or owner", "sleepy": "suspended or waiting",
        "taken": "linked to another wiki account", "moved": "linked to another hosting account",
        "frozen": "suspended or not approved", "waiting": "suspended or not approved", "done": "Already linked",
    }
    for name, text in expectations.items():
        assert text in _row(page, name), name
    assert "Link 1 account" in page and fields["mode"] == "username" and len(fields["token"]) == 64


def test_several_candidates_are_a_conflict(web, site, make_account, query):
    tenant = site["tenant"]
    verified = {"email_verified_at": DAY}
    make_account("twin1", email="twins@example.org", **verified)
    make_account("twin2", **verified)
    query("UPDATE accounts SET email = 'twins@example.org' WHERE username = 'twin2'")
    _wiki_user(tenant, "pair", email="twins@example.org")
    make_account("single", email="single@example.org", **verified)
    _wiki_user(tenant, "first", email="single@example.org")
    _wiki_user(tenant, "second", email="single@example.org")      # the same account for two wiki accounts
    make_account("plain", email="plain@example.org")              # address not verified: never matched
    _wiki_user(tenant, "unverified", email="plain@example.org")
    _response, page, _fields = _preview(web, site, mode="email")
    for name in ("pair", "first", "second"):
        assert "Several accounts match" in _row(page, name), name
    assert "Not found" in _row(page, "unverified")
    assert "Nothing to link" in page


def test_apply_links_both_sides_and_logs_it(web, site, make_account, query):
    from bananawiki.hosting import email

    ann = make_account("ann", email="ann@example.org")
    _wiki_user(site["tenant"], "ann")
    boss = make_account("boss")
    _wiki_user(site["tenant"], "boss", "owner")
    _response, page, fields = _preview(web, site, mode="username", include_admins="1")
    assert "Link 2 accounts" in page
    with email.capture_outbox() as sent:
        response = web.post(_url(site, "/apply"), data={**fields, "notify": "1"})
    assert response.status_code == 302
    assert site["tenant"].links["ann"]["account_id"] == ann["id"]
    assert site["tenant"].links["boss"]["account_id"] == boss["id"]
    links = query("SELECT account_id, wiki_user_id, wiki_username FROM hosting_oauth_account_links ORDER BY "
                  "wiki_username")
    assert [(row["account_id"], row["wiki_user_id"]) for row in links] == [(ann["id"], "ann"), (boss["id"], "boss")]
    assert query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'oauth.linked'", one=True)["n"] == 2
    logged = query("SELECT reason, actor_id FROM hosting_events WHERE action = 'oauth.admin_linked' ORDER BY id")
    assert [row["reason"] for row in logged] == ["ann -> ann (username)", "boss -> boss (username)"]
    assert {row["actor_id"] for row in logged} == {site["admin"]["id"]}
    assert [message.to for message in sent] == ["ann@example.org"]
    assert "now connected" in sent[0].subject


def test_a_stale_preview_is_refused_and_recomputed(web, site, make_account, query):
    ann = make_account("ann")
    _wiki_user(site["tenant"], "ann")
    _response, _page, fields = _preview(web, site, mode="username")
    _portal_link(query, site, ann, "elsewhere")  # the portal changed after the preview
    response = web.post(_url(site, "/apply"), data=fields)
    assert response.status_code == 409
    page = response.get_data(as_text=True)
    assert "changed since the preview" in page
    assert "linked to another wiki account" in _row(page, "ann")
    assert "ann" not in site["tenant"].links
    forged = web.post(_url(site, "/apply"), data={**fields, "token": "0" * 64})
    assert forged.status_code == 409


def test_one_account_and_a_list(web, site, make_account, query):
    verified = {"email_verified_at": DAY}
    dana = make_account("dana", email="dana@example.org", **verified)
    eve = make_account("eve-portal")
    _wiki_user(site["tenant"], "dana-wiki")
    _wiki_user(site["tenant"], "eve")
    _response, page, fields = _preview(web, site, mode="one", wiki_username="DANA-wiki", account="dana@example.org")
    assert "Will link" in _row(page, "dana-wiki")
    web.post(_url(site, "/apply"), data=fields)
    assert site["tenant"].links["dana-wiki"]["account_id"] == dana["id"]
    listed = f"eve,{eve['id']}\nmissing,eve-portal\n# a comment\n"
    _response, page, fields = _preview(web, site, mode="list", list=listed)
    assert "Will link" in _row(page, "eve") and "No such account on the wiki" in _row(page, "missing")
    web.post(_url(site, "/apply"), data=fields)
    assert site["tenant"].links["eve"]["account_id"] == eve["id"]
    bad, _page, _fields = _preview(web, site, mode="list", list="just-one-cell")
    assert bad.status_code == 400


def test_unlink_removes_both_sides(web, site, make_account, query):
    ann = make_account("ann")
    _wiki_user(site["tenant"], "ann")
    _response, _page, fields = _preview(web, site, mode="username")
    web.post(_url(site, "/apply"), data=fields)
    assert web.post(_url(site, "/unlink"), data={"wiki_user_id": "ann"}).status_code == 302
    assert "ann" not in site["tenant"].links
    assert query("SELECT COUNT(*) AS n FROM hosting_oauth_account_links", one=True)["n"] == 0
    assert query("SELECT subject_id FROM hosting_events WHERE action = 'oauth.unlinked'", one=True)[
        "subject_id"] == ann["id"]
    assert query("SELECT reason FROM hosting_events WHERE action = 'oauth.admin_unlinked'", one=True)["reason"] == "ann"


def test_hosting_only_is_refused_while_an_administrator_is_unlinked(web, site, make_account, query):
    tenant = site["tenant"]
    owner_name = next(iter(tenant.users))  # the wiki's first administrator, made with the wiki
    make_account("ann")
    _wiki_user(tenant, "ann")
    response = web.post(_url(site, "/hosting-only"), follow_redirects=True)
    assert owner_name in response.get_data(as_text=True) and "Not yet" in response.get_data(as_text=True)
    assert query("SELECT platform_signin_mode FROM instances WHERE id = ?", (site["wiki"]["id"],), one=True)[
        "platform_signin_mode"] in ("", None)
    _response, _page, fields = _preview(web, site, mode="one", wiki_username=owner_name,
                                        account=site["owner"]["username"], include_admins="1")
    web.post(_url(site, "/apply"), data=fields)
    assert web.post(_url(site, "/hosting-only")).status_code == 302
    assert query("SELECT platform_signin_mode FROM instances WHERE id = ?", (site["wiki"]["id"],), one=True)[
        "platform_signin_mode"] == "platform"


def test_only_administrators_reach_the_tool(portal, site, make_account, login):
    client = portal.test_client()
    login(client, site["owner"])
    assert client.get(_url(site)).status_code == 403
    assert client.post(_url(site, "/unlink"), data={"wiki_user_id": "x"}).status_code == 403


def test_the_page_has_no_save(web, site):
    from tests.save_controls import save_controls

    _wiki_user(site["tenant"], "ann")
    assert save_controls(web.get(_url(site)).get_data(as_text=True)) == []


# The wiki's side: the real tenant task through the runtime agent


def _wiki_db(runtime, name: str = "acme") -> sqlite3.Connection:
    conn = sqlite3.connect(Path(runtime._cfg().instances_dir) / name / "bananawiki.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_the_wiki_writes_the_links_itself(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    runtime.set_user_password(spec, "member", "member-password", "user")
    accounts, total = runtime.list_links(spec)
    assert total == 2 and {account.username for account in accounts} == {"owner1", "member"}
    member = next(account for account in accounts if account.username == "member")
    assert member.link is None and member.status == "active" and member.password_set
    with closing(_wiki_db(runtime)) as conn, conn:
        conn.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at, "
                     "auth_method) VALUES ('s1', 't1', ?, '2026-01-01', '2026-01-01', '2099-01-01', "
                     "'platform_oauth')", (member.id,))
    linked, refused = runtime.link_users(spec, [(member.id, "acct-9", "member9"), ("missing", "acct-8", "x")],
                                         actor="chief", rule="list")
    assert linked == [member.id] and refused == {"missing": "user_not_found"}
    again, why = runtime.link_users(spec, [(member.id, "acct-7", "y")], actor="chief", rule="one")
    assert again == [] and why == {member.id: "user_linked"}, "never replaced"
    owner = next(account for account in accounts if account.username == "owner1")
    _linked, why = runtime.link_users(spec, [(owner.id, "acct-9", "z")], actor="chief", rule="one")
    assert why == {owner.id: "account_linked"}
    with closing(_wiki_db(runtime)) as conn:
        row = conn.execute("SELECT * FROM platform_oauth_links").fetchone()
        assert (row["account_id"], row["user_id"], row["portal_username"]) == ("acct-9", member.id, "member9")
        audit = conn.execute("SELECT details FROM audit_log WHERE action = 'user.platform_linked'").fetchone()
        assert '"hosting_admin": "chief"' in audit["details"] and '"rule": "list"' in audit["details"]
    assert runtime.list_links(spec)[0][1].link["account_id"] == "acct-9"
    assert runtime.unlink_user(spec, member.id, actor="chief") == "acct-9"
    with closing(_wiki_db(runtime)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM platform_oauth_links").fetchone()[0] == 0
        assert conn.execute("SELECT revoked_at FROM user_sessions WHERE id = 's1'").fetchone()[0]
    assert runtime.unlink_user(spec, member.id, actor="chief") is None


def test_a_link_to_a_wiki_account_that_is_gone_can_be_dropped(web, site, make_account, query):
    _portal_link(query, site, make_account("ann"), "deleted-user")
    assert web.post(_url(site, "/unlink"), data={"wiki_user_id": "deleted-user"}).status_code == 302
    assert query("SELECT COUNT(*) AS n FROM hosting_oauth_account_links", one=True)["n"] == 0
