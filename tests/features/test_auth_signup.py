"""Sign-up: invite codes, open sign-up, approval, bot protection and rate limits."""

from __future__ import annotations

import re

import pytest
from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import bot_protection


@pytest.fixture
def no_bots(db):
    db.execute("UPDATE site_settings SET bot_protection_enabled = 0")


def _invite(db, creator, code="ABCD-1234", **values):
    columns = {"code": code, "created_by": creator["id"], "max_uses": 1, "use_count": 0}
    columns.update(values)
    db.insert("invite_codes", columns)
    return code


def _signup(client, username="newbie", invite=None, password=PASSWORD, confirm=None, **extra):
    data = {"username": username, "password": password,
            "confirm_password": password if confirm is None else confirm}
    if invite is not None:
        data["invite_code"] = invite
    data.update(extra)
    return client.post("/signup", data=data)


def test_signup_requires_invite_when_closed(client, db, admin, no_bots):
    response = _signup(client)
    assert response.status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    response = _signup(client, invite="NOPE-NOPE")
    assert response.status_code == 400


def test_invite_is_case_insensitive_and_tolerates_missing_hyphen(client, db, admin, no_bots):
    _invite(db, admin, max_uses=0)
    assert _signup(client, "alpha", invite="abcd-1234").status_code == 302
    assert _signup(client, "bravo", invite="abcd1234").status_code == 302
    assert _signup(client, "charlie", invite=" ab cd 1234 ").status_code == 302
    usage = db.scalar("SELECT COUNT(*) FROM invite_code_usage")
    assert usage == 3
    assert db.scalar("SELECT use_count FROM invite_codes") == 3
    assert db.scalar("SELECT invite_code FROM users WHERE username = 'bravo'") == "ABCD-1234"


def test_eight_character_custom_code(client, db, admin, no_bots):
    _invite(db, admin, code="BANANA42")
    assert _signup(client, invite="banana42").status_code == 302


def test_invite_max_uses_and_expiry(client, db, admin, no_bots):
    _invite(db, admin)
    assert _signup(client, "first", invite="ABCD-1234").status_code == 302
    assert _signup(client, "second", invite="ABCD-1234").status_code == 400
    _invite(db, admin, code="OLDC-ODE1", expires_at="2000-01-01 00:00:00", max_uses=5)
    assert _signup(client, "third", invite="OLDC-ODE1").status_code == 400
    _invite(db, admin, code="GONE-CODE", deleted=1)
    assert _signup(client, "fourth", invite="GONE-CODE").status_code == 400


def test_invite_from_suspended_creator_is_refused(client, db, make_user, no_bots):
    creator = make_user("creator", role="admin", suspended=1)
    _invite(db, creator)
    assert _signup(client, invite="ABCD-1234").status_code == 400


def test_invite_assigns_role_and_custom_role(client, db, admin, no_bots):
    _invite(db, admin, assigned_role="editor")
    assert _signup(client, "eddie", invite="ABCD-1234").status_code == 302
    assert db.scalar("SELECT role FROM users WHERE username = 'eddie'") == "editor"
    role_id = db.insert("custom_roles", {"name": "Writers", "base_role": "editor"})
    _invite(db, admin, code="ROLE-CODE", assigned_role="user", assigned_custom_role_id=role_id)
    assert _signup(client, "writer", invite="ROLE-CODE").status_code == 302
    row = db.one("SELECT role, custom_role_id FROM users WHERE username = 'writer'")
    assert row == {"role": "editor", "custom_role_id": role_id}


def test_failed_account_creation_does_not_consume_invite(client, db, admin, make_user, no_bots):
    make_user("taken")
    _invite(db, admin)
    assert _signup(client, "taken", invite="ABCD-1234").status_code == 400
    assert db.scalar("SELECT use_count FROM invite_codes") == 0
    assert db.scalar("SELECT COUNT(*) FROM invite_code_usage") == 0


def test_username_and_password_rules(client, db, no_bots):
    db.execute("UPDATE site_settings SET open_signup = 1")
    assert _signup(client, "a b").status_code == 400
    assert _signup(client, "admin").status_code == 400  # reserved
    assert _signup(client, "shorty", password="short").status_code == 400
    assert _signup(client, "mismatch", confirm="something else").status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM users") == 0


def test_open_signup_and_expiry(client, db, no_bots):
    db.execute("UPDATE site_settings SET open_signup = 1, open_signup_until = '2000-01-01 00:00:00'")
    assert _signup(client).status_code == 400
    db.execute("UPDATE site_settings SET open_signup_until = '2999-01-01 00:00:00'")
    response = _signup(client)
    assert response.status_code == 302
    assert db.scalar("SELECT approval_status FROM users WHERE username = 'newbie'") == "approved"


def test_approval_required_creates_pending_account_that_cannot_use_the_wiki(client, db, login, no_bots):
    db.execute("UPDATE site_settings SET open_signup = 1, approval_required = 1")
    assert _signup(client).status_code == 302
    user = db.one("SELECT * FROM users WHERE username = 'newbie'")
    assert user["approval_status"] == "pending"
    login(client, user)
    assert client.get("/_probe/private").headers["Location"].endswith("/account-status")


def test_signup_closed_during_maintenance(client, db, no_bots):
    db.execute("UPDATE site_settings SET open_signup = 1, maintenance_mode = 1")
    assert client.get("/signup").headers["Location"].endswith("/maintenance")
    assert _signup(client).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM users") == 0


def test_signed_in_users_are_sent_home(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/signup").status_code == 302


def test_signup_is_rate_limited_per_address(client, db, no_bots):
    for _ in range(10):
        _signup(client, "x y")
    assert _signup(client, "valid_name").status_code == 429


def test_bot_protection_blocks_missing_token_and_honeypot(client, db):
    db.execute("UPDATE site_settings SET open_signup = 1, bot_protection_enabled = 1")
    assert _signup(client).status_code == 400
    page = client.get("/signup")
    token = re.search(rb'name="_form_time" value="([^"]+)"', page.data).group(1).decode()
    assert _signup(client, website="http://spam", _form_time=token).status_code == 400


def _token_from(client) -> str:
    page = client.get("/signup")
    return re.search(rb'name="_form_time" value="([^"]+)"', page.data).group(1).decode()


def test_bot_protection_rejects_forged_and_replayed_tokens(app_factory):
    app = app_factory(environ={"BW_MIN_FORM_SECONDS": "0"})
    client = app.test_client()
    with app.app_context(), connection_scope() as session:
        session.execute("UPDATE site_settings SET open_signup = 1, bot_protection_enabled = 1")
    token = _token_from(client)
    forged = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert _signup(client, "bot1", _form_time=forged).status_code == 400
    assert _signup(client, "human1", _form_time=token).status_code == 302
    assert _signup(client, "human2", _form_time=token).status_code == 400  # replayed


def _rejection(app, token: str) -> str | None:
    with app.test_request_context("/signup", method="POST", data={"_form_time": token}), connection_scope():
        return bot_protection.rejection()


def test_bot_protection_minimum_and_maximum_form_age(app, db):
    db.execute("UPDATE site_settings SET bot_protection_enabled = 1")
    with app.test_request_context():
        token = bot_protection.new_token()
        issued, nonce, _signature = token.split(".")
        old = f"{int(issued) - 3 * 3600 * 1000}.{nonce}"
        stale = f"{old}.{bot_protection._signature(old)}"
    assert _rejection(app, token) == "too_fast"
    assert _rejection(app, stale) == "expired_token"
