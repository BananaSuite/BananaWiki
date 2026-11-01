"""Re-audit pass 17 (XW1-x, XW2-x) regressions."""
import pytest

from bananawiki.wiki.features.auth import platform_oauth
from tests.features.test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start


@pytest.fixture
def mallory_portal(monkeypatch):
    fake = FakePortal()
    fake.profile = {"sub": "acct-mallory", "username": "mallory"}
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


def test_xw1_1_invite_codes_tried_through_platform_sign_up_share_the_signup_budget(app_factory, mallory_portal):
    from bananawiki.core.sqlite import Session
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    app = app_factory(environ=OAUTH_ENV)
    db = Session(app.extensions["bananawiki.database"].connect())
    db.execute("UPDATE site_settings SET open_signup = 0")
    with app.test_request_context(), connection_scope():
        admin = accounts.create("boss", "correct horse battery", role="owner", emit_event=False)
    db.execute("INSERT INTO invite_codes (code, created_by, created_at, max_uses, assigned_role) "
               "VALUES ('STAFF2026', ?, '2026-01-01 00:00:00', 0, 'admin')", (admin["id"],))
    browser = app.test_client()
    _callback(browser, _start(browser))
    statuses = [browser.post("/platform-oauth/signup", data={"username": "mallory", "invite_code": f"STAFF{year}"})
                .status_code for year in range(2000, 2010)]
    assert statuses == [400] * 10
    assert browser.post("/platform-oauth/signup",
                        data={"username": "mallory", "invite_code": "STAFF2026"}).status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'mallory'") == 0
    assert app.test_client().post("/signup", data={"username": "g", "password": "x", "confirm_password": "x",
                                                   "invite_code": "STAFF2026"}).status_code == 429


def test_xw2_1_a_reset_after_a_change_of_owner_withdraws_federation_shares(app, make_user, db):
    import json

    from bananawiki.ops import tenant_task
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.pages import service as pages

    owner = make_user("wikiadmin", role="owner")
    with app.test_request_context(), connection_scope():
        page = pages.create("Shared notes", "body", author_id=owner["id"])
    db.execute("INSERT INTO federation_peers (wiki_id, name, base_url, secret, generation) "
               "VALUES ('peer1', 'Old wiki', 'https://previous-owner.example/', 'x', 'g')")
    db.execute("INSERT INTO federation_shares (peer_id, page_id, granted_by) VALUES ('peer1', ?, ?)",
               (page["id"], owner["id"]))
    request = {"username": "wikiadmin", "password": "Temporary pass 67890"}
    assert tenant_task.reset_admin(app.config["BW"], request)["federation_shares_withdrawn"] == 0
    assert tenant_task.reset_admin(app.config["BW"], {**request, "every_admin": True})["federation_shares_withdrawn"] == 1
    assert db.scalar("SELECT COUNT(*) FROM federation_shares") == 0
    assert db.scalar("SELECT COUNT(*) FROM federation_peers") == 1
    entry = db.one("SELECT * FROM audit_log WHERE action = 'federation.page_unshared'")
    assert entry["target_id"] == str(page["id"]) and json.loads(entry["details"])["via"] == "hosting portal"


def test_yw1_1_the_reset_drops_saved_original_passwords(app, make_user, db):
    from bananawiki.ops import tenant_task

    make_user("wikiadmin", role="owner")
    backdoor = make_user("backdoor", role="admin")
    db.execute("UPDATE users SET original_password_backup = password WHERE id = ?", (backdoor["id"],))
    tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 67890",
                                               "every_admin": True})
    assert db.scalar("SELECT original_password_backup FROM users WHERE id = ?", (backdoor["id"],)) is None
