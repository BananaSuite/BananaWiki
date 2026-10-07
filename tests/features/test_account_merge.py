"""Account merges: owners request and confirm, administrators approve."""

from __future__ import annotations

import json

from conftest import PASSWORD

from .people_support import make_page


def _request(client, other: str, direction: str = "into_mine", password: str = PASSWORD):
    return client.post("/settings/merge-request", data={"other_username": other, "direction": direction,
                                                        "reason": "old account", "password": password})


def _setup_confirmed(app, make_user, login, db):
    """old (source) into new (target), confirmed by both owners."""
    new, old = make_user("newbob"), make_user("oldbob")
    first, second = app.test_client(), app.test_client()
    login(first, new)
    _request(first, "oldbob")
    merge_id = db.scalar("SELECT id FROM account_merge_requests")
    login(second, old)
    second.post(f"/settings/merge/approve/{merge_id}", data={"password": PASSWORD})
    return new, old, merge_id


def test_request_needs_password_and_existing_user(client, make_user, login, db):
    login(client, make_user("newbob"))
    make_user("oldbob")
    _request(client, "oldbob", password="wrong-password")
    _request(client, "nobody")
    _request(client, "newbob")
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests") == 0


def test_request_records_both_sides(client, make_user, login, db):
    new, old = make_user("newbob"), make_user("oldbob")
    login(client, new)
    assert _request(client, "oldbob").headers["Location"].endswith("/settings/merge/pending")
    row = db.one("SELECT * FROM account_merge_requests")
    assert (row["source_user_id"], row["target_user_id"]) == (old["id"], new["id"])
    assert row["target_approved"] == 1 and row["source_approved"] == 0 and row["status"] == "pending"
    assert db.scalar("SELECT pending_merge_source_id FROM users WHERE id = ?", (new["id"],)) == old["id"]
    _request(client, "oldbob")
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests") == 1
    assert b"@oldbob into @newbob" in client.get("/settings/merge/pending").data


def test_other_owner_confirms_with_password(app, make_user, login, db):
    new, old, merge_id = _setup_confirmed(app, make_user, login, db)
    assert db.scalar("SELECT status FROM account_merge_requests WHERE id = ?", (merge_id,)) == "approved_by_both"


def test_outsiders_cannot_confirm_or_cancel(app, client, make_user, login, db):
    new, _old = make_user("newbob"), make_user("oldbob")
    first = app.test_client()
    login(first, new)
    _request(first, "oldbob")
    merge_id = db.scalar("SELECT id FROM account_merge_requests")
    login(client, make_user("eve"))
    client.post(f"/settings/merge/approve/{merge_id}", data={"password": PASSWORD})
    client.post(f"/settings/merge/cancel/{merge_id}")
    row = db.one("SELECT * FROM account_merge_requests WHERE id = ?", (merge_id,))
    assert row["status"] == "pending" and row["source_approved"] == 0


def test_party_can_deny(app, client, make_user, login, db):
    new, old = make_user("newbob"), make_user("oldbob")
    first = app.test_client()
    login(first, new)
    _request(first, "oldbob")
    merge_id = db.scalar("SELECT id FROM account_merge_requests")
    login(client, old)
    client.post(f"/settings/merge/deny/{merge_id}")
    assert db.scalar("SELECT status FROM account_merge_requests WHERE id = ?", (merge_id,)) == "denied"
    assert db.scalar("SELECT pending_merge_source_id FROM users WHERE id = ?", (new["id"],)) is None


def test_admin_cannot_approve_before_both_confirm(app, admin_client, make_user, login, db):
    new, _old = make_user("newbob"), make_user("oldbob")
    first = app.test_client()
    login(first, new)
    _request(first, "oldbob")
    merge_id = db.scalar("SELECT id FROM account_merge_requests")
    admin_client.post(f"/admin/merge-requests/{merge_id}/approve")
    assert db.scalar("SELECT status FROM account_merge_requests WHERE id = ?", (merge_id,)) == "pending"


def test_admin_approval_moves_data_and_deletes_source(app, make_user, login, db, admin):
    new, old, merge_id = _setup_confirmed(app, make_user, login, db)
    page = make_page(app, "Notes", "Ask @oldbob", author_id=old["id"])
    db.execute("INSERT INTO user_profiles (user_id, real_name) VALUES (?, 'Old Bob')", (old["id"],))
    db.execute("INSERT INTO user_custom_tags (user_id, label) VALUES (?, 'veteran')", (old["id"],))
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps({"theme_mode": "light"}), old["id"]))
    badge = db.insert("badge_types", {"name": "B"})
    for user in (new, old):
        db.execute("INSERT INTO user_badges (user_id, badge_type_id) VALUES (?, ?)", (user["id"], badge))
    boss = app.test_client()
    login(boss, admin)
    assert b"oldbob" in boss.get("/admin/merge-requests").data
    boss.post(f"/admin/merge-requests/{merge_id}/approve", data={"delete_source": "1"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (old["id"],)) is None
    assert db.scalar("SELECT edited_by FROM page_history WHERE page_id = ?", (page["id"],)) == new["id"]
    assert db.scalar("SELECT real_name FROM user_profiles WHERE user_id = ?", (new["id"],)) == "Old Bob"
    assert db.scalar("SELECT label FROM user_custom_tags WHERE user_id = ?", (new["id"],)) == "veteran"
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (new["id"],)) == 1
    assert json.loads(db.scalar("SELECT accessibility FROM users WHERE id = ?", (new["id"],)))["theme_mode"] == "light"
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "Ask @newbob"
    # The request and log reference the deleted account and go with it (ON DELETE CASCADE in 1.4's schema).
    assert db.scalar("SELECT COUNT(*) FROM account_merge_requests") == 0


def test_locked_source_is_suspended_and_renamed(app, make_user, login, db, admin):
    new, old, merge_id = _setup_confirmed(app, make_user, login, db)
    db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, 'page.view_all')", (old["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (old["id"],))
    boss = app.test_client()
    login(boss, admin)
    boss.post(f"/admin/merge-requests/{merge_id}/approve")
    assert db.scalar("SELECT status FROM account_merge_requests WHERE id = ?", (merge_id,)) == "merged"
    assert db.scalar("SELECT merged_by FROM account_merge_logs") == admin["id"]
    row = db.one("SELECT username, suspended FROM users WHERE id = ?", (old["id"],))
    assert row["username"] == "merged_oldbob" and row["suspended"] == 1
    # Demoted like any other account: no overrides left to come back on reactivation.
    for table in ("user_permissions", "user_category_access"):
        assert db.scalar(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (old["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                     (old["id"],)) == 0


def test_merge_pages_are_admin_only(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/admin/merge-requests").status_code == 403
    assert client.post("/admin/users/merge", data={"source_username": "a", "target_username": "b"}).status_code == 403


def test_admin_direct_merge_preview_and_execute(app, admin_client, make_user, db):
    source, target = make_user("src_user"), make_user("dst_user")
    make_page(app, "Doc", "x", author_id=source["id"])
    preview = admin_client.post("/admin/users/merge", data={"source_username": "src_user",
                                                            "target_username": "dst_user", "action": "preview"})
    assert b"page_history.edited_by" in preview.data
    admin_client.post("/admin/users/merge", data={"source_username": "src_user", "target_username": "dst_user",
                                                  "action": "execute", "delete_source": "1"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (source["id"],)) is None
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE edited_by = ?", (target["id"],)) == 1


def test_protected_accounts_cannot_be_merged(admin_client, make_user, db):
    source = make_user("keeper", is_superuser=1)
    make_user("dst_user")
    admin_client.post("/admin/users/merge", data={"source_username": "keeper", "target_username": "dst_user",
                                                  "action": "execute", "delete_source": "1"})
    assert db.scalar("SELECT 1 FROM users WHERE id = ?", (source["id"],))
