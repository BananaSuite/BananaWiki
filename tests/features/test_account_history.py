"""Former user names and suspension records, which renames, deletions and merges must respect."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import PASSWORD

from bananawiki.wiki import accounts
from bananawiki.wiki.features.admin import service as administration
from bananawiki.wiki.features.pages import service as pages
from bananawiki.wiki.features.users import merge
from bananawiki.wiki.features.users import service as profiles

from .people_support import in_app, make_category, make_page, publish, restrict_read


def _rename(client, new_username: str, password: str = PASSWORD):
    return client.post("/settings/username", data={"new_username": new_username, "password": password})


def _username(db, user) -> str:
    return db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],))


def _suspend(app, actor, target) -> None:
    in_app(app, lambda: administration.suspend(actor, target, until=None, label="permanent", reason="",
                                               reason_visible=False, time_visible=False))


# ── Renames and deletions leave pages alone ───────────────────────────────────


def test_renames_and_deletion_do_not_touch_pages_the_user_cannot_read(app, client, make_user, login, db):
    teacher = make_user("teacher", role="editor")
    staff = make_category(app, "Staff room")
    page = make_page(app, "Marking", "Use @Override and @param.", author_id=teacher["id"],
                     category_id=staff["id"])
    db.execute("INSERT INTO drafts (page_id, user_id, content) VALUES (?, ?, 'Draft: @Override')",
               (page["id"], teacher["id"]))
    student = make_user("student")
    restrict_read(db, student, [])
    login(client, student)
    for name in ("Override", "pwned", "param"):
        _rename(client, name)
    assert _username(db, student) == "param"
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (student["id"],)) is None
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "Use @Override and @param."
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == 1
    assert db.scalar("SELECT content FROM drafts WHERE page_id = ?", (page["id"],)) == "Draft: @Override"


def test_self_renames_are_limited_per_day(client, make_user, login, db):
    user = make_user("bob")
    make_user("taken_name")
    login(client, user)
    _rename(client, "bob_wrong", password="wrong-password")
    _rename(client, "taken_name")
    _rename(client, "bob")
    for name in ("bob_1", "bob_2", "bob_3"):
        _rename(client, name)
    assert _username(db, user) == "bob_3", "refused and unchanged renames are not counted"
    _rename(client, "bob_4")
    assert _username(db, user) == "bob_3"
    assert b"at most 3 times a day" in client.get("/settings").data


# ── Former names ──────────────────────────────────────────────────────────────


def test_old_profile_links_follow_the_rename(client, make_user, login, db):
    alice = make_user("alice")
    login(client, alice)
    _rename(client, "alice_new")
    client.post("/logout")
    login(client, make_user("eve"))
    assert client.get("/users/alice").status_code == 404, "an unpublished profile does not reveal the new name"
    publish(db, alice)
    assert client.get("/users/ALICE").headers["Location"].endswith("/users/alice_new")
    assert client.get("/users/nobody").status_code == 404


def test_former_names_stay_with_their_account(app, client, make_user, login, db):
    alice, mallory = make_user("alice"), make_user("mallory")
    login(client, alice)
    _rename(client, "alice_new")
    with pytest.raises(accounts.AccountError, match="username_taken"):
        make_user("Alice")
    client.post("/logout")
    login(client, mallory)
    _rename(client, "alice")
    assert _username(db, mallory) == "mallory"
    with pytest.raises(accounts.AccountError, match="username_taken"):
        in_app(app, lambda: accounts.rename(accounts.by_id(mallory["id"]), "alice"))
    client.post("/logout")
    login(client, {**alice, "username": "alice_new"})
    _rename(client, "alice")
    assert _username(db, alice) == "alice", "the account itself may take its old name back"
    in_app(app, lambda: accounts.delete(accounts.by_id(alice["id"])))
    assert make_user("alice")["username"] == "alice", "deleting the account frees its names"


def test_a_rename_from_a_stale_copy_records_the_name_given_up(app, make_user, db):
    alice = make_user("alice")
    in_app(app, lambda: accounts.rename(alice, "alice_two"))
    in_app(app, lambda: accounts.rename(alice, "alice_three"))  # another tab, still showing "alice"
    history = db.all("SELECT old_username, new_username FROM username_history WHERE user_id = ? ORDER BY id",
                     (alice["id"],))
    assert [(row["old_username"], row["new_username"]) for row in history] == [
        ("alice", "alice_two"), ("alice_two", "alice_three")]
    with pytest.raises(accounts.AccountError, match="username_taken"):
        make_user("alice_two")


# ── Merges ────────────────────────────────────────────────────────────────────


def test_merge_rewrites_mentions_without_stopping_at_a_page_that_cannot_take_them(app, make_user, admin, db):
    source, target = make_user("src"), make_user("a_much_longer_target_name")
    short = make_page(app, "Short", "Ask @src")
    filler = "@src " * (pages.MAX_CONTENT // 5)
    full = make_page(app, "Full", filler)
    in_app(app, lambda: merge.execute(source, target, admin, delete_source=True))
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (source["id"],)) is None
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (short["id"],)) == "Ask @a_much_longer_target_name"
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (full["id"],)) == filler


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_merge_lock_leaves_nothing_to_sign_in_or_reactivate_with(app, client, make_user, login, db, role):
    make_user("first_owner", role="owner")
    boss = make_user("boss", role="owner")
    old, new = make_user("bob_old", role=role), make_user("bob_new")
    db.execute("INSERT INTO temp_roles (user_id, original_role, expires_at) VALUES (?, ?, '2099-01-01 00:00:00')",
               (old["id"], role))
    # Requested and confirmed by both accounts: an owner is changed only with its consent.
    request_row = in_app(app, lambda: merge.create(new, old, into_mine=True, reason="", password=PASSWORD))
    in_app(app, lambda: merge.confirm(request_row["id"], old, PASSWORD))
    in_app(app, lambda: merge.approve(request_row["id"], boss, delete_source=False))
    locked = db.one("SELECT * FROM users WHERE id = ?", (old["id"],))
    assert locked["username"] == "merged_bob_old" and locked["suspended"] == 1 and locked["role"] == "user"
    assert db.scalar("SELECT 1 FROM temp_roles WHERE user_id = ?", (old["id"],)) is None
    audit = db.one("SELECT * FROM suspension_audit WHERE user_id = ?", (old["id"],))
    assert (audit["action"], audit["performed_by"], audit["imposed_by_top"]) == ("suspend", boss["id"], 1)
    assert db.scalar("SELECT new_role FROM role_history WHERE user_id = ?", (old["id"],)) == "user"
    response = client.post("/login", data={"username": "merged_bob_old", "password": PASSWORD})
    assert response.status_code != 302, "the old password no longer works"
    # Even an administrator account in this state could not lift the suspension itself.
    assert not in_app(app, lambda: profiles.may_reactivate_self({**locked, "role": "admin"}))


@pytest.mark.parametrize("delete_source", [True, False])
def test_merge_checks_the_last_owner_again_in_its_transaction(app, make_user, admin, db, monkeypatch,
                                                               delete_source):
    first, second = make_user("first_owner", role="owner"), make_user("second_owner", role="owner")
    target = make_user("second_new")
    page = make_page(app, "Notes", "x", author_id=second["id"])
    request_row = in_app(app, lambda: merge.create(target, second, into_mine=True, reason="", password=PASSWORD))
    in_app(app, lambda: merge.confirm(request_row["id"], second, PASSWORD))
    check = merge._check_pair

    def other_owner_steps_down(source, target_):
        # Passes, then the other owner steps down before the merge transaction starts.
        check(source, target_)
        if accounts.owners_count() == 2:
            profiles.set_owner_status(first, PASSWORD)

    monkeypatch.setattr(merge, "_check_pair", other_owner_steps_down)
    with pytest.raises(merge.MergeError, match="last_owner"):
        in_app(app, lambda: merge.approve(request_row["id"], admin, delete_source=delete_source))
    assert db.one("SELECT role, suspended FROM users WHERE id = ?", (second["id"],)) == {"role": "owner",
                                                                                       "suspended": 0}
    assert db.scalar("SELECT COUNT(*) FROM users WHERE role = 'owner'") == 1
    assert db.scalar("SELECT edited_by FROM page_history WHERE page_id = ?", (page["id"],)) == second["id"]
    assert db.scalar("SELECT status FROM account_merge_requests") == "approved_by_both"
    assert db.scalar("SELECT COUNT(*) FROM account_merge_logs") == 0


def test_a_direct_merge_checks_the_administrators_rights_again(app, make_user, admin, db, monkeypatch):
    source, target = make_user("promoted"), make_user("dst_user")
    hash_password = accounts.hash_password

    def promoted_meanwhile(password):
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (source["id"],))
        return hash_password(password)

    monkeypatch.setattr(accounts, "hash_password", promoted_meanwhile)
    with pytest.raises(merge.MergeError, match="admin_needs_owner"):
        in_app(app, lambda: merge.execute(source, target, admin, delete_source=False))
    assert db.one("SELECT username, role, suspended FROM users WHERE id = ?", (source["id"],)) == {
        "username": "promoted", "role": "admin", "suspended": 0}


def test_a_request_is_merged_once(app, make_user, admin, db):
    source, target = make_user("oldbob"), make_user("newbob")
    request_row = in_app(app, lambda: merge.create(source, target, into_mine=False, reason="", password=PASSWORD))
    in_app(app, lambda: merge.confirm(request_row["id"], target, PASSWORD))
    in_app(app, lambda: merge.execute(source, target, admin, delete_source=False, merge_id=request_row["id"]))
    # A second administrator approving the same request at the same time.
    with pytest.raises(merge.MergeError, match="not_pending"):
        in_app(app, lambda: merge.execute(source, target, admin, delete_source=False, merge_id=request_row["id"]))
    assert _username(db, source) == "merged_oldbob"
    assert db.scalar("SELECT COUNT(*) FROM account_merge_logs") == 1


def test_the_last_active_administrator_cannot_be_merged_away(app, make_user, admin, db):
    other = make_user("admin_other")
    with pytest.raises(merge.MergeError, match="last_admin"):
        in_app(app, lambda: merge.execute(admin, other, admin, delete_source=True))
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (admin["id"],))
    make_user("colleague", role="admin")
    in_app(app, lambda: merge.execute(admin, other, admin, delete_source=True))
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (admin["id"],)) is None


def test_deleting_the_source_keeps_the_background_the_target_took_over(app, make_user, admin, db):
    name = f"backgrounds/{'a' * 32}.jpg"
    path = Path(app.config["BW"].folders.uploads) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg")
    source, target = make_user("src"), make_user("dst")
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps({"background_image": name}),
                                                                    source["id"]))
    in_app(app, lambda: merge.execute(source, target, admin, delete_source=True))
    assert json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (target["id"],)))[
        "background_image"] == name
    assert path.exists()
    assert db.scalar("SELECT actor_id FROM audit_log WHERE action = 'user.deleted' AND target_id = ?",
                     (source["id"],)) == admin["id"]


# ── Self-reactivation fails closed ────────────────────────────────────────────


def test_suspension_records_the_rank_of_whoever_imposed_it(app, make_user, db):
    owner, moderator = make_user("first_owner", role="owner"), make_user("moderator", role="admin")
    editor, admin_target = make_user("editor_ed", role="editor"), make_user("boss", role="admin")
    _suspend(app, moderator, editor)
    _suspend(app, owner, admin_target)
    rows = {row["user_id"]: row["imposed_by_top"] for row in db.all("SELECT * FROM suspension_audit")}
    assert rows == {editor["id"]: 0, admin_target["id"]: 1}


@pytest.mark.parametrize("change", ["demoted", "deleted"])
def test_an_owners_suspension_outlives_the_owner(app, client, make_user, login, db, change):
    make_user("first_owner", role="owner")
    second = make_user("second_owner", role="owner")
    boss = make_user("boss", role="admin")
    _suspend(app, second, boss)
    if change == "demoted":
        db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (second["id"],))
    else:
        db.execute("DELETE FROM users WHERE id = ?", (second["id"],))
    login(client, boss)
    assert b"Reactivate my account" not in client.get("/account-status").data
    assert client.post("/settings/reactivate").status_code == 403
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (boss["id"],)) == 1


@pytest.mark.parametrize("record", ["kept", "suspender_deleted", "no_audit", "latest_is_unsuspend"])
def test_self_reactivation_needs_a_known_administrator_suspension(app, client, make_user, login, db, record):
    moderator = make_user("moderator", role="admin")
    boss = make_user("boss", role="editor")
    _suspend(app, moderator, boss)
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (boss["id"],))
    if record == "suspender_deleted":
        db.execute("DELETE FROM users WHERE id = ?", (moderator["id"],))
    elif record == "no_audit":
        db.execute("DELETE FROM suspension_audit")
    elif record == "latest_is_unsuspend":
        db.execute("INSERT INTO suspension_audit (user_id, action, performed_by, created_at) "
                   "VALUES (?, 'unsuspend', ?, '2999-01-01 00:00:00')", (boss["id"], moderator["id"]))
    login(client, boss)
    client.post("/settings/reactivate")
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (boss["id"],)) == (0 if record == "kept" else 1)
