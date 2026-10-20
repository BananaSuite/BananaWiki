"""API tokens cannot be used to gain more access than their owner has."""

from __future__ import annotations

import pytest

from bananawiki.wiki.features.api_service import tokens

from .api_support import call, enable_api, issue
from .pages_support import in_app

NEW_PASSWORD = "BrandNewPassw0rd!!"
PASSWORD = "correct horse battery"


@pytest.fixture
def api_app(app, db):
    enable_api(app, db)
    return app


# Accounts


def test_a_token_cannot_set_its_own_accounts_password_without_the_current_one(api_app, client, make_user):
    owner = make_user("boss", role="owner")
    token = issue(api_app, owner, ["users"])
    refused = call(client, "PUT", f"/users/{owner['id']}", token, json={"password": NEW_PASSWORD})
    assert refused.status_code == 403 and refused.json["code"] == "current_password_required"
    wrong = call(client, "PUT", f"/users/{owner['id']}", token,
                 json={"password": NEW_PASSWORD, "current_password": "not my password"})
    assert wrong.status_code == 403 and wrong.json["code"] == "current_password_wrong"
    assert client.post("/login", data={"username": "boss", "password": NEW_PASSWORD}).status_code != 302
    changed = call(client, "PUT", f"/users/{owner['id']}", token,
                   json={"password": NEW_PASSWORD, "current_password": PASSWORD})
    assert changed.status_code == 200 and changed.json["api_tokens_revoked"] >= 1


def test_administrators_still_reset_other_passwords(api_app, client, make_user, admin):
    member = make_user("member1")
    token = issue(api_app, admin, ["users"])
    assert call(client, "PUT", f"/users/{member['id']}", token, json={"password": NEW_PASSWORD}).status_code == 200


def test_tokens_never_create_owners_or_superusers(api_app, client, db, make_user):
    owner = make_user("boss", role="owner")
    token = issue(api_app, owner, ["users"])
    assert call(client, "POST", "/users", token,
                json={"username": "newboss", "password": NEW_PASSWORD, "role": "owner"}).status_code == 400
    made = call(client, "POST", "/users", token,
                json={"username": "sneaky", "password": NEW_PASSWORD, "role": "admin", "is_superuser": True})
    assert made.status_code == 201
    assert db.scalar("SELECT is_superuser FROM users WHERE username = 'sneaky'") == 0
    member = make_user("member2")
    assert call(client, "PUT", f"/users/{member['id']}", token, json={"role": "owner"}).status_code == 403
    assert call(client, "PUT", f"/users/{member['id']}", token, json={"is_superuser": True}).status_code == 200
    assert db.scalar("SELECT is_superuser FROM users WHERE id = ?", (member["id"],)) == 0


# Tokens minted by tokens


def _mint(client, parent, scopes):
    return call(client, "POST", "/tokens", parent,
                json={"name": "child", "permissions": {"read": True, "write": True, "scopes": scopes}})


def test_revoking_a_token_revokes_the_tokens_it_minted(api_app, client, db, admin):
    parent = issue(api_app, admin, ["pages", "tokens"])
    minted = _mint(client, parent, ["pages"])
    assert minted.status_code == 201
    child = minted.json["token"]
    assert call(client, "GET", "/pages", child).status_code == 200
    parent_id = db.scalar("SELECT parent_id FROM api_service__tokens WHERE id = ?", (minted.json["id"],))
    assert parent_id is not None
    in_app(api_app, lambda: tokens.revoke(parent_id))
    assert call(client, "GET", "/pages", parent).status_code == 401
    assert call(client, "GET", "/pages", child).status_code == 401


def test_revoking_through_the_api_and_token_page_cascades(api_app, client, db, login, admin):
    parent = issue(api_app, admin, ["pages", "tokens"])
    child = _mint(client, parent, ["pages"]).json["token"]
    parent_id = db.scalar("SELECT id FROM api_service__tokens WHERE parent_id IS NULL")
    login(client, admin)
    client.post(f"/settings/api-tokens/{parent_id}/revoke")
    assert call(client, "GET", "/pages", child).status_code == 401
    other = issue(api_app, admin, ["pages", "tokens"])
    grandchild = _mint(client, other, ["pages"]).json["token"]
    other_id = db.scalar("SELECT id FROM api_service__tokens WHERE active = 1 AND parent_id IS NULL")
    assert call(client, "DELETE", f"/tokens/{other_id}", other).status_code == 200
    assert call(client, "GET", "/pages", grandchild).status_code == 401


def test_minted_tokens_cannot_mint_tokens(api_app, client, admin):
    parent = issue(api_app, admin, ["pages", "tokens"])
    refused = _mint(client, parent, ["pages", "tokens"])
    assert refused.status_code == 403 and refused.json["code"] == "tokens_scope_forbidden"


# Userbot lock


def test_an_ordinary_token_cannot_bypass_the_userbot_lock(api_app, client, db, login, make_user):
    member = make_user("botty", api_access_enabled=1)
    db.execute("UPDATE users SET userbot_mode_lock = 'force_disabled', userbot_enabled = 0 WHERE id = ?",
               (member["id"],))
    token = issue(api_app, member, ["userbot"], name="mybot")
    assert call(client, "GET", "/userbot/me", token).json["code"] == "userbot_disabled"
    assert call(client, "POST", "/userbot/profile", token, json={"bio": "still here"}).status_code == 403


def test_the_userbot_scope_is_not_offered_on_ordinary_tokens(api_app, client, login, make_user):
    member = make_user("botty", api_access_enabled=1)
    login(client, member)
    page = client.get("/settings/api-tokens").get_data(as_text=True)
    assert 'value="userbot"' not in page
    parent = issue(api_app, member, ["pages", "tokens", "userbot"])
    refused = _mint(client, parent, ["userbot"])
    assert refused.status_code == 403 and refused.json["code"] == "userbot_scope_reserved"
    assert tokens.scopes_for_role(member, ["pages", "userbot"]) == ["pages"]


# The token page


def test_the_token_page_shows_which_tokens_a_token_issued(api_app, client, db, make_user, login):
    owner = make_user("scripter", role="editor")
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (owner["id"],))
    grant = tokens.Grant(True, True, ("pages", "tokens"))
    _raw, ci = in_app(api_app, lambda: tokens.create(owner["id"], name="ci", grant=grant))
    _raw, child = in_app(api_app, lambda: tokens.create(owner["id"], name="deploy", grant=tokens.Grant(
        True, True, ("pages",)), parent_id=ci))
    in_app(api_app, lambda: tokens.create(owner["id"], name="", grant=tokens.Grant(True, False, ("pages",)),
                                          parent_id=child))
    in_app(api_app, lambda: tokens.create(owner["id"], name="laptop", grant=tokens.Grant(True, False, ("pages",))))
    login(client, owner)
    page = client.get("/settings/api-tokens").get_data(as_text=True)
    assert "Issued through the API by the token “ci”" in page
    assert "Issued through the API by the token “deploy”" in page
    assert "Revoking a token also revokes every token it issued" in page
    # The confirmation names how many tokens go with it (the grandchild too).
    assert "the 2 tokens it issued are revoked with it" in page
    assert "and the token it issued is revoked with it" in page
    response = client.post(f"/settings/api-tokens/{ci}/revoke")
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE active = 1") == 1
    assert "Token revoked, with the 2 tokens it issued." in client.get("/settings/api-tokens").get_data(as_text=True)
