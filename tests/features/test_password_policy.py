"""New passwords: no common passwords, nothing built from the user name or the email address.

The policy applies wherever a password is chosen (sign-up, settings, the
forced change, administrators creating accounts or setting passwords, the
REST API, the command line); signing in with an existing password is never
affected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope

from .api_support import call, enable_api, issue

COMMON = "password123"
PERSONAL = "must not contain your username"  # from auth.error.password_personal
TRANSLATIONS = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/auth/translations"


@pytest.fixture
def open_signup(db):
    db.execute("UPDATE site_settings SET bot_protection_enabled = 0, open_signup = 1 WHERE id = 1")


def _signup(client, username, password):
    return client.post("/signup", data={"username": username, "password": password, "confirm_password": password})


def _flashes(client) -> str:
    return client.get("/settings").get_data(as_text=True)


def test_sign_up_refuses_common_and_personal_passwords(client, db, open_signup):
    refused = _signup(client, "newbie", COMMON)
    assert refused.status_code == 400 and "too common" in refused.get_data(as_text=True)
    refused = _signup(client, "newbie", "Newbie-2026!")
    assert refused.status_code == 400 and PERSONAL in refused.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    assert _signup(client, "newbie", "a quiet violet harbour").status_code == 302


def test_changing_the_password_in_the_settings(client, make_user, login, db):
    bob = make_user("bobby")
    db.execute("UPDATE users SET email = 'rossi.mario@example.com' WHERE id = ?", (bob["id"],))
    login(client, bob)
    for weak, message in ((COMMON, "too common"), ("bobby-the-great", PERSONAL),
                          ("rossi.mario.2026", "email address")):
        response = client.post("/settings/password", data={"current_password": PASSWORD, "new_password": weak,
                                                            "confirm_password": weak})
        assert response.status_code == 302
        assert message in _flashes(client), weak
    assert db.scalar("SELECT password FROM users WHERE id = ?", (bob["id"],)) == bob["password"]
    client.post("/settings/password", data={"current_password": PASSWORD, "new_password": "a quiet violet harbour",
                                            "confirm_password": "a quiet violet harbour"})
    assert db.scalar("SELECT password FROM users WHERE id = ?", (bob["id"],)) != bob["password"]


def test_the_forced_change_checks_the_new_name(client, make_user, login, db):
    bob = make_user("bobby", force_password_change=1)
    login(client, bob)
    response = client.post("/force-change-password", data={
        "current_password": PASSWORD, "username": "harbourmaster", "new_password": "harbourmaster-01",
        "confirm_password": "harbourmaster-01"})
    assert response.status_code == 200 and PERSONAL in response.get_data(as_text=True)
    assert db.scalar("SELECT username FROM users WHERE id = ?", (bob["id"],)) == "bobby"


def test_administrators_creating_accounts_and_setting_passwords(admin_client, make_user, db):
    response = admin_client.post("/admin/users/create", data={
        "username": "newbie", "password": COMMON, "confirm_password": COMMON, "role": "user"})
    assert "too common" in response.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    member = make_user("member1")
    admin_client.post(f"/admin/users/{member['id']}/edit", data={
        "action": "change_password", "password": "member1-password", "confirm_password": "member1-password"})
    assert db.scalar("SELECT password FROM users WHERE id = ?", (member["id"],)) == member["password"]
    assert PERSONAL in admin_client.get(f"/admin/users/{member['id']}").get_data(as_text=True)


def test_the_rest_api(app, client, db, make_user, admin):
    enable_api(app, db)
    token = issue(app, admin, ["users"])
    made = call(client, "POST", "/users", token, json={"username": "newbie", "password": COMMON})
    assert made.status_code == 400 and made.json["code"] == "password_common"
    member = make_user("member1")
    changed = call(client, "PUT", f"/users/{member['id']}", token, json={"password": "Member1-and-more"})
    assert changed.status_code == 400 and changed.json["code"] == "password_personal"
    assert db.scalar("SELECT password FROM users WHERE id = ?", (member["id"],)) == member["password"]


def test_existing_weak_passwords_still_sign_in(app, client, make_user, login, db):
    """Accounts that chose a weak password before the policy keep working."""
    from bananawiki.wiki import accounts

    bob = make_user("bobby")
    with app.test_request_context(), connection_scope():
        weak = accounts.hash_password(COMMON)
    db.execute("UPDATE users SET password = ? WHERE id = ?", (weak, bob["id"]))
    login(client, bob, password=COMMON)


def test_messages_exist_in_english_and_italian():
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((TRANSLATIONS / "it.json").read_text(encoding="utf-8"))
    for key in ("auth.error.password_common", "auth.error.password_personal", "auth.hint.password"):
        assert english[key] and italian[key] and english[key] != italian[key], key
        assert set(re.findall(r"{(\w+)}", english[key])) == set(re.findall(r"{(\w+)}", italian[key])), key
