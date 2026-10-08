# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Former user names stay reserved for their account until an administrator releases them."""

from __future__ import annotations

import json

import pytest
from conftest import PASSWORD

from bananawiki.wiki import accounts

from .people_support import in_app


def _rename(app, user, new_username: str) -> None:
    in_app(app, lambda: accounts.rename(accounts.by_id(user["id"]), new_username))


def _release(client, user, username: str):
    return client.post(f"/admin/users/{user['id']}/attributions",
                       data={"action": "release_name", "username": username}, follow_redirects=True)


def _former_names(db, user) -> list[str]:
    return db.column("SELECT old_username FROM username_history WHERE user_id = ? ORDER BY id", (user["id"],))


def _create(client, username: str):
    return client.post("/admin/users/create", data={"username": username, "password": PASSWORD,
                                                    "confirm_password": PASSWORD, "role": "user"})


def test_an_administrator_releases_a_former_name(app, admin_client, admin, make_user, db):
    bob = make_user("bob")
    for name in ("bobby", "bob", "robert"):  # "bob" given up twice
        _rename(app, bob, name)
    with pytest.raises(accounts.AccountError, match="username_taken"):
        make_user("bob")
    audit = admin_client.get(f"/admin/users/{bob['id']}/audit").get_data(as_text=True)
    assert "Former names reserved for this account" in audit
    assert 'name="username" value="bob"' in audit and 'name="username" value="bobby"' in audit
    assert admin_client.get("/users/bob").headers["Location"].endswith("/users/robert")

    page = _release(admin_client, bob, "BOB").get_data(as_text=True)
    assert "bob was released" in page
    assert _former_names(db, bob) == ["bobby"], "every rename away from the name goes"
    entry = db.one("SELECT * FROM audit_log WHERE action = 'user.name_released'")
    assert entry["actor_id"] == admin["id"] and entry["target_id"] == bob["id"]
    assert json.loads(entry["details"]) == {"username": "robert", "released_name": "bob"}
    assert admin_client.get("/users/bob").status_code == 404, "old links no longer lead to the account"
    assert make_user("bob")["username"] == "bob"
    with pytest.raises(accounts.AccountError, match="username_taken"):
        make_user("bobby")


def test_only_names_reserved_for_the_account_are_released(app, admin_client, make_user, db):
    bob = make_user("bob")
    _rename(app, bob, "bobby")
    _rename(app, bob, "bob")  # took its old name back: "bob" is in use, "bobby" is reserved
    for name in ("bob", "nobody", ""):
        assert "is not a former name reserved for this account" in _release(admin_client, bob, name).get_data(
            as_text=True)
    eve = make_user("eve")
    assert "is not a former name reserved" in _release(admin_client, eve, "bobby").get_data(as_text=True)
    assert _former_names(db, bob) == ["bob", "bobby"]
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'user.name_released'") == 0


def test_releasing_follows_the_administrator_hierarchy(app, admin_client, make_user, login, db):
    peer = make_user("peer_admin", role="admin")
    _rename(app, peer, "peer_admin_2")
    audit = admin_client.get(f"/admin/users/{peer['id']}/audit").get_data(as_text=True)
    assert "Former names reserved for this account" in audit and 'value="release_name"' not in audit
    _release(admin_client, peer, "peer_admin")
    assert _former_names(db, peer) == ["peer_admin"]

    owner = make_user("the_owner", role="owner")
    owner_client = app.test_client()
    login(owner_client, owner)
    _release(owner_client, peer, "peer_admin")
    assert _former_names(db, peer) == []
    assert make_user("peer_admin")["username"] == "peer_admin"


def test_members_cannot_release_names(app, client, make_user, login, db):
    bob = make_user("bob")
    _rename(app, bob, "bobby")
    login(client, make_user("eddie", role="editor"))
    assert client.post(f"/admin/users/{bob['id']}/attributions",
                       data={"action": "release_name", "username": "bob"}).status_code == 403
    assert _former_names(db, bob) == ["bob"]


def test_administrators_learn_which_account_keeps_a_name(app, admin_client, make_user, db):
    alice, carol = make_user("alice"), make_user("carol")
    _rename(app, alice, "alice_new")
    page = _create(admin_client, "Alice").get_data(as_text=True)
    assert "Alice is a former name of alice_new and stays reserved for that account" in page
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'Alice' COLLATE NOCASE") == 0
    page = admin_client.post(f"/admin/users/{carol['id']}/edit", data={"action": "change_username",
                                                                       "username": "alice"},
                             follow_redirects=True).get_data(as_text=True)
    assert "alice is a former name of alice_new" in page
    assert "That username is already taken." in _create(admin_client, "carol").get_data(as_text=True)


def _legacy_renames(db, *rows) -> None:
    """Renames as 1.4 recorded them: it did not reserve former names, so several accounts may share one."""
    db.executemany("INSERT INTO username_history (user_id, old_username, new_username, changed_at) "
                   "VALUES (?, ?, ?, ?)", rows)


def test_a_name_given_up_by_several_accounts_passes_to_the_one_before(app, admin_client, make_user, db):
    alpha, beta = make_user("alpha"), make_user("beta")
    _legacy_renames(db, (alpha["id"], "bob", "alpha", "2020-01-01 00:00:00"),
                    (beta["id"], "bob", "beta", "2021-01-01 00:00:00"))
    assert admin_client.get("/users/bob").headers["Location"].endswith("/users/beta")

    page = _release(admin_client, beta, "bob").get_data(as_text=True)
    assert "bob no longer leads to this account, but it is also a former name of alpha" in page
    assert "was released" not in page
    assert _former_names(db, beta) == []
    assert _former_names(db, alpha) == ["bob"], "the other account is left alone"
    assert in_app(app, lambda: accounts.username_taken("bob"))
    assert in_app(app, lambda: accounts.reserved_names(alpha["id"])) == ["bob"]
    assert admin_client.get("/users/bob").headers["Location"].endswith("/users/alpha")
    entry = db.one("SELECT details FROM audit_log WHERE action = 'user.name_released'")
    assert json.loads(entry["details"]) == {"username": "beta", "released_name": "bob",
                                            "still_reserved_for": "alpha"}

    audit = admin_client.get(f"/admin/users/{alpha['id']}/audit").get_data(as_text=True)
    assert 'name="username" value="bob"' in audit
    assert "bob was released" in _release(admin_client, alpha, "bob").get_data(as_text=True)
    assert make_user("bob")["username"] == "bob"


def test_only_ascii_letters_are_folded_as_in_sqlite(app, admin_client, make_user, db):
    bob = make_user("bob")
    _legacy_renames(db, (bob["id"], "élodie", "Élodie", "2020-01-01 00:00:00"),
                    (bob["id"], "Élodie", "bob", "2021-01-01 00:00:00"))
    assert in_app(app, lambda: accounts.reserved_names(bob["id"])) == ["Élodie", "élodie"]
    assert "élodie was released" in _release(admin_client, bob, "élodie").get_data(as_text=True)
    assert _former_names(db, bob) == ["Élodie"]
    assert "Élodie was released" in _release(admin_client, bob, "ÉLODIE").get_data(as_text=True)
    assert _former_names(db, bob) == []
