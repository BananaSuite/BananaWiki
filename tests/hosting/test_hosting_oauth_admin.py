"""Reviewing apps, the per-wiki sign-in mode, the operator command line and the schema of the apps feature."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bananawiki.hosting.db import connection_scope

from .hosting_support import REDIRECT
from .hosting_support import count as _count

# Reviewing apps


def test_the_review_pages_are_for_administrators(web, make_app, make_account, login):
    owner = make_account()
    app = make_app(owner)
    login(web, owner)
    for response in (web.get("/admin/apps"), web.get(f"/admin/apps/{app['id']}"),
                     web.post(f"/admin/apps/{app['id']}/review", data={"decision": "approve"}),
                     web.post(f"/admin/apps/{app['id']}/delete")):
        assert response.status_code in (302, 403, 404)
    assert web.get("/dashboard").status_code == 200


def test_a_non_administrator_cannot_approve_their_own_app(web, make_app, make_account, login, query):
    owner = make_account()
    app = make_app(owner)
    login(web, owner)
    web.post(f"/admin/apps/{app['id']}/review", data={"decision": "approve"})
    assert query("SELECT status FROM hosting_oauth_apps", one=True)["status"] == "pending"


def test_an_administrator_reviews_and_the_owner_is_told(web, make_app, make_account, login, query):
    admin, owner = make_account(admin=True), make_account()
    app = make_app(owner)
    login(web, admin)
    listing = web.get("/admin/apps?status=pending").get_data(as_text=True)
    assert "BananaGit" in listing and app["client_id"] in listing
    assert "Apps waiting for review" in web.get("/admin").get_data(as_text=True)
    assert web.post(f"/admin/apps/{app['id']}/review", data={"decision": "bogus"}).status_code == 302
    assert query("SELECT status FROM hosting_oauth_apps", one=True)["status"] == "pending"
    done = web.post(f"/admin/apps/{app['id']}/review", data={"decision": "approve", "note": "Looks fine"})
    assert done.status_code == 302
    assert query("SELECT status, review_note FROM hosting_oauth_apps", one=True) == {
        "status": "approved", "review_note": "Looks fine"}
    notices = query("SELECT source_id, outcome, detail FROM hosting_account_notices WHERE account_id = ?",
                    (owner["id"],))
    assert notices == [{"source_id": "apps.review", "outcome": "approved", "detail": "BananaGit"}]
    detail = web.get(f"/admin/apps/{app['id']}").get_data(as_text=True)
    assert "Looks fine" in detail and REDIRECT in detail
    assert query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'oauth.app.approved'", one=True)["n"] == 1


def test_the_owner_sees_the_notice_text(portal, make_app, make_account):
    admin, owner = make_account(admin=True), make_account()
    app = make_app(owner)
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import notifications, oauth_apps
        from bananawiki.hosting.db import db

        oauth_apps.review(oauth_apps.get(app["id"]), "deny", actor_id=admin["id"], note="Unclear")
        notice = db.one("SELECT * FROM hosting_account_notices WHERE account_id = ?", (owner["id"],))
        text = notifications.notice_text(notice)
    assert "BananaGit" in text and "not approved" in text


def test_the_pending_count_is_in_the_attention_queues(portal, make_app, make_account):
    owner = make_account()
    make_app(owner, "One")
    make_app(owner, "Two", status="approved")
    with portal.test_request_context("/"), connection_scope():
        from bananawiki.hosting import attention

        assert attention.counts()["apps.review"] == 1


def test_settings_page_saves_the_app_policy(web, make_account, login, query, oauth_on):
    login(web, make_account(admin=True))
    response = web.post("/admin/settings", data={"action": "save_oauth", "platform_oauth_enabled": "1",
                                                 "oauth_apps_per_account": "3"})
    assert response.status_code == 302
    row = query("SELECT * FROM hosting_settings", one=True)
    assert (row["platform_oauth_enabled"], row["oauth_apps_enabled"], row["oauth_apps_per_account"]) == (1, 0, 3)
    web.post("/admin/settings", data={"action": "save_oauth", "oauth_apps_per_account": "-1"})
    assert query("SELECT oauth_apps_per_account AS n FROM hosting_settings", one=True)["n"] == 3


# Sign-in mode per wiki


def _link(query, wiki, account):
    query("INSERT INTO hosting_oauth_account_links (instance_id, account_id, wiki_user_id, wiki_username) "
          "VALUES (?, ?, 'u1', 'someone')", (wiki["id"], account["id"]))


def test_the_portal_decides_a_wikis_sign_in_mode_and_the_wiki_gets_it(portal, make_account, make_wiki, query,
                                                                     oauth_on):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    owner = make_account(admin=True)
    wiki = make_wiki(owner, "modewiki")
    _link(query, wiki, owner)
    with portal.test_request_context("/"), connection_scope():
        assert instances.policy(instances.get(wiki["id"])).oauth.signin_mode == ""
        assert instances.set_signin_mode(instances.get(wiki["id"]), "platform", actor_id=owner["id"]) is True
        assert instances.policy(instances.get(wiki["id"])).oauth.signin_mode == "platform"
        with pytest.raises(ServiceError):
            instances.set_signin_mode(instances.get(wiki["id"]), "everyone", actor_id=owner["id"])
        assert instances.set_signin_mode(instances.get(wiki["id"]), "", actor_id=owner["id"]) is True
        assert instances.policy(instances.get(wiki["id"])).oauth.signin_mode == ""
    assert query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'oauth.signin_mode'", one=True)["n"] == 2


def test_hosting_accounts_only_is_refused_while_nobody_is_linked(portal, make_account, make_wiki, query,
                                                                oauth_on):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    owner = make_account(admin=True)
    wiki = make_wiki(owner, "nolinks")
    with portal.test_request_context("/"), connection_scope():
        with pytest.raises(ServiceError) as error:
            instances.set_signin_mode(instances.get(wiki["id"]), "platform", actor_id=owner["id"])
        assert error.value.key == "hosting.instances.signin_mode_nobody_linked"
        assert instances.set_signin_mode(instances.get(wiki["id"]), "both", actor_id=owner["id"]) is True
        assert instances.set_signin_mode(instances.get(wiki["id"]), "platform", actor_id=None, force=True) is True


def test_the_tenant_gets_the_mode_in_its_environment():
    from bananawiki.hosting.runtime import OAuthClient

    assert OAuthClient("bw_x", "secret", "https://portal.example", "id1").signin_mode == ""
    assert OAuthClient("bw_x", "secret", "https://portal.example", "id1", "both").signin_mode == "both"
    from bananawiki.hosting.runtime import agent

    assert "BW_PLATFORM_SIGNIN_MODE" in Path(agent.__file__).read_text(encoding="utf-8")


def test_administrators_set_the_mode_from_the_portal_and_others_cannot(portal, web, make_account, make_wiki, login,
                                                                      query, oauth_on):
    admin, owner = make_account(admin=True), make_account()
    wiki = make_wiki(owner, "adminmode")
    _link(query, wiki, owner)
    login(web, owner)
    refused = web.post(f"/admin/instances/{wiki['id']}/signin-mode", data={"mode": "platform"})
    assert refused.status_code in (302, 403, 404)
    assert query("SELECT platform_signin_mode AS m FROM instances", one=True)["m"] == ""
    other = portal.test_client()
    login(other, admin)
    assert other.post(f"/admin/instances/{wiki['id']}/signin-mode", data={"mode": "platform"}).status_code == 302
    assert query("SELECT platform_signin_mode AS m FROM instances", one=True)["m"] == "platform"
    assert 'value="platform" selected' in other.get(f"/admin/instances/{wiki['id']}").get_data(as_text=True)
    other.post(f"/admin/instances/{wiki['id']}/signin-mode", data={"mode": "nonsense"})
    assert query("SELECT platform_signin_mode AS m FROM instances", one=True)["m"] == "platform"


# Command line


@pytest.fixture
def cli(portal, capsys, monkeypatch):
    from bananawiki.hosting import admin

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = admin.main(list(argv), app=portal)
        out, err = capsys.readouterr()
        return code, out, err

    return run


def test_the_command_line_registers_and_reviews_apps(cli, make_account, query, oauth_on):
    make_account("owner")
    code, out, _err = cli("--json", "app", "create", "owner", "Forge", "--redirect-uri", REDIRECT, "--pending")
    created = json.loads(out)
    assert code == 0 and created["status"] == "pending" and created["client_secret"]
    assert "client_secret_hash" not in created
    code, out, _err = cli("app", "approve", created["client_id"], "--note", "ok")
    assert code == 0 and "approved" in out
    code, out, _err = cli("app", "list", "--status", "approved")
    assert code == 0 and created["client_id"] in out and created["client_secret"] not in out
    code, out, _err = cli("--json", "app", "show", created["id"])
    assert "client_secret_hash" not in json.loads(out)
    code, out, _err = cli("--json", "app", "rotate-secret", created["id"])
    assert code == 0 and json.loads(out)["client_secret"] != created["client_secret"]
    assert cli("app", "delete", created["id"])[0] == 1  # needs --yes
    assert cli("app", "delete", created["id"], "--yes")[0] == 0
    assert _count(query, "hosting_oauth_apps") == 0
    assert cli("app", "show", "nothing")[0] == 1


def test_the_command_line_sets_a_wikis_sign_in_mode(cli, make_account, make_wiki, query, oauth_on):
    owner = make_account("wikiowner")
    wiki = make_wiki(owner, "climode")
    assert cli("instance", "signin-mode", "climode", "local")[0] == 0
    assert query("SELECT platform_signin_mode AS m FROM instances", one=True)["m"] == "local"
    code, _out, err = cli("instance", "signin-mode", "climode", "platform")
    assert code == 1 and "recovery" in err
    assert cli("instance", "signin-mode", "climode", "platform", "--force")[0] == 0
    _link(query, wiki, owner)
    assert cli("instance", "signin-mode", "climode", "wiki")[0] == 0
    assert query("SELECT platform_signin_mode AS m FROM instances", one=True)["m"] == ""


def test_the_secret_is_never_in_the_audit_log(cli, portal, make_account, oauth_on):
    from bananawiki.hosting import admin

    make_account("owner")
    _code, out, _err = cli("--json", "app", "create", "owner", "Forge", "--redirect-uri", REDIRECT)
    secret = json.loads(out)["client_secret"]
    audit = Path(portal.config["HOSTING"].database_path).parent / admin.AUDIT_FILE
    assert audit.exists() and secret not in audit.read_text()


# Schema


def test_the_schema_has_the_new_tables_and_columns(portal, query):
    def columns(table):
        with portal.app_context(), connection_scope():
            from bananawiki.hosting.db import db

            return {row["name"] for row in db.all(f"PRAGMA table_info({table})")}

    tables = {row["name"] for row in query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"hosting_oauth_apps", "hosting_oauth_app_grants"} <= tables
    assert "scope" in columns("hosting_oauth_authorization_codes")
    assert "platform_signin_mode" in columns("instances")
    settings = query("SELECT oauth_apps_enabled AS e, oauth_apps_per_account AS n FROM hosting_settings", one=True)
    assert settings == {"e": 1, "n": 5}


def test_the_migration_runs_on_a_v11_database_and_twice(tmp_path):
    from bananawiki.core.sqlite import execute_script
    from bananawiki.hosting.migrations import _BASELINE_SQL, MIGRATIONS, v12_oauth_apps

    conn = sqlite3.connect(tmp_path / "old.db")
    execute_script(conn, _BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO hosting_settings (id, signup_mode) VALUES (1, 'open')")
    # Every migration before v12 (later ones may follow it in MIGRATIONS), then v12 twice.
    for migration in MIGRATIONS[:MIGRATIONS.index(v12_oauth_apps.upgrade)]:
        migration(conn)
    assert "hosting_oauth_apps" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    v12_oauth_apps.upgrade(conn)
    v12_oauth_apps.upgrade(conn)
    assert conn.execute("SELECT oauth_apps_enabled, oauth_apps_per_account FROM hosting_settings").fetchone() == (1, 5)
    assert conn.execute("SELECT COUNT(*) FROM hosting_oauth_apps").fetchone() == (0,)
