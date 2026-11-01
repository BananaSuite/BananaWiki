"""Regression tests for the HW1 findings of the full re-audit pass 3 (kanban revert, disabled profiles,
page protection switch)."""

from __future__ import annotations

# HW1-1: a kanban revert obeys the W4-1 board caps


def _board_with_writer(app, client, make_user, login, db):
    """A board owned by an administrator, shared for writing with a plain user signed in on *client*."""
    owner = app.test_client()
    login(owner, make_user("boss", role="admin"))
    mallory = make_user("mallory", role="user")
    assert owner.post("/kanban/create", data={"title": "B"}).status_code == 302
    board_id = db.scalar("SELECT MAX(id) FROM kanban_boards")
    assert owner.post(f"/kanban/{board_id}/share", data={"action": "add_user", "user_id": mallory["id"],
                                                         "access_level": "write"}).status_code == 302
    login(client, mallory)
    column_id = db.scalar("SELECT id FROM kanban_columns WHERE board_id = ? ORDER BY sort_order LIMIT 1", (board_id,))
    return board_id, column_id


def _tickets(db, board_id):
    return db.column("SELECT t.id FROM kanban_tickets t JOIN kanban_columns c ON c.id = t.column_id "
                     "WHERE c.board_id = ? ORDER BY t.id", (board_id,))


def _create(client, column_id, title, description=""):
    response = client.post(f"/api/kanban/columns/{column_id}/tickets", json={"title": title, "description": description})
    assert response.status_code == 201, response.get_json()


def _delete_all(client, db, board_id):
    assert client.post(f"/api/kanban/{board_id}/tickets/bulk",
                       json={"action": "delete", "ticket_ids": _tickets(db, board_id)}).status_code == 200


def test_hw1_1_revert_obeys_the_ticket_cap_and_does_not_multiply_tickets(app, client, make_user, login, db,
                                                                         monkeypatch):
    from bananawiki.wiki.features.kanban import fields

    monkeypatch.setattr(fields, "MAX_TICKETS_PER_BOARD", 4)
    board_id, column_id = _board_with_writer(app, client, make_user, login, db)
    for n in range(3):
        _create(client, column_id, f"t{n}")
    snapshot = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board_id,))
    original = _tickets(db, board_id)
    _delete_all(client, db, board_id)

    # Reverting to the same version again and again brings the deleted tickets back once.
    for _ in range(3):
        assert client.post(f"/kanban/{board_id}/revert/{snapshot}").status_code == 302
        assert _tickets(db, board_id) == original

    # A revert that would pass the cap is refused and changes nothing.
    _delete_all(client, db, board_id)
    for n in range(3):
        _create(client, column_id, f"new{n}")
    kept = _tickets(db, board_id)
    response = client.post(f"/kanban/{board_id}/revert/{snapshot}", follow_redirects=True)
    assert b"at most 4 tickets" in response.data
    assert _tickets(db, board_id) == kept


def test_hw1_1_revert_obeys_the_text_cap(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.kanban import fields

    monkeypatch.setattr(fields, "MAX_BOARD_BYTES", 25_000)
    board_id, column_id = _board_with_writer(app, client, make_user, login, db)
    for n in range(2):
        _create(client, column_id, f"t{n}", "d" * 9_000)
    snapshot = db.scalar("SELECT MAX(id) FROM kanban_board_history WHERE board_id = ?", (board_id,))
    _delete_all(client, db, board_id)
    _create(client, column_id, "new", "d" * 9_000)
    kept = _tickets(db, board_id)

    response = client.post(f"/kanban/{board_id}/revert/{snapshot}", follow_redirects=True)

    assert b"MB of text" in response.data
    assert _tickets(db, board_id) == kept


# HW1-2: deleting a disabled profile keeps the administrator's block


def test_hw1_2_deleting_a_disabled_profile_keeps_the_block(app, client, make_user, login, db):
    from tests.features.api_support import call, enable_api, issue

    enable_api(app, db)
    administrator, reader = app.test_client(), app.test_client()
    login(administrator, make_user("boss", role="admin"))
    login(reader, make_user("reader"))
    mallory = make_user("mallory", api_access_enabled=1, userbot_enabled=1)
    login(client, mallory)
    assert administrator.post("/admin/profile-fields", data={"action": "create", "label": "Website",
                                                             "field_type": "text"}).status_code == 302
    field_id = db.scalar("SELECT id FROM user_profile_fields__definitions")
    client.post("/settings/profile-fields", data={f"value_{field_id}": "abusive field", f"visible_{field_id}": "1"})
    client.post("/settings/profile/publish", data={"published": "1"})
    assert reader.get("/users/mallory").status_code == 200
    assert administrator.post(f"/admin/users/{mallory['id']}/profile",
                              data={"action": "disable_profile"}).status_code == 302

    assert client.post("/settings/profile/delete").status_code == 302
    client.post("/settings/profile/publish", data={"published": "1"})
    key = issue(app, mallory, ["userbot"], name="userbot")
    assert call(client, "POST", "/userbot/profile", key, json={"page_published": True}).status_code == 403

    assert reader.get("/users/mallory").status_code == 404
    assert db.one("SELECT page_published, page_disabled_by_admin FROM user_profiles WHERE user_id = ?",
                  (mallory["id"],)) == {"page_published": 0, "page_disabled_by_admin": 1}
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__values WHERE user_id = ?", (mallory["id"],)) == 0


def test_hw1_2_deleting_an_own_profile_removes_it_with_its_field_answers(app, client, make_user, login, db):
    administrator = app.test_client()
    login(administrator, make_user("boss", role="admin"))
    member = make_user("member")
    login(client, member)
    administrator.post("/admin/profile-fields", data={"action": "create", "label": "Website", "field_type": "text"})
    field_id = db.scalar("SELECT id FROM user_profile_fields__definitions")
    client.post("/settings/profile", data={"real_name": "M", "bio": "bio"})
    client.post("/settings/profile-fields", data={f"value_{field_id}": "mine", f"visible_{field_id}": "1"})
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__values WHERE user_id = ?", (member["id"],)) == 1

    assert client.post("/settings/profile/delete").status_code == 302

    assert db.scalar("SELECT COUNT(*) FROM user_profiles WHERE user_id = ?", (member["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM user_profile_fields__values WHERE user_id = ?", (member["id"],)) == 0


# HW1-3: switching page protection off while pages are protected


_NUMBERS = {"page_reservation_duration_hours": "24", "page_reservation_cooldown_hours": "0",
            "default_reserved_pages_quota": "5", "reservation_quota_auto_approve_max": "0",
            "quota_request_cooldown_hours": "0"}


def _protected_page(app, db, controller):
    from tests.features.pages_support import make_page, set_feature

    page = make_page(app, "Guarded", "text")
    set_feature(app, "page_governance", True)
    set_feature(app, "audit", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    db.execute("UPDATE pages SET protected_by = ?, protected_at = '2025-01-01 00:00:00' WHERE id = ?",
               (controller["id"], page["id"]))
    return page


def _switches(db):
    return db.column("SELECT details FROM audit_log WHERE action = 'page_governance.protection_switched' ORDER BY id")


def test_hw1_3_administrators_cannot_switch_protection_off_while_others_protect_pages(app, client, login,
                                                                                     make_user, db):
    from tests.features.api_support import call, enable_api, issue
    from tests.features.pages_support import get_page

    controller = make_user("controller", role="editor")
    administrator = make_user("plainadmin", role="admin")
    page = _protected_page(app, db, controller)
    login(client, administrator)

    response = client.post("/admin/governance", data=_NUMBERS, follow_redirects=True)
    assert b"unlock delay" in response.data
    enable_api(app, db)
    key = issue(app, administrator, ["settings"], name="settings")
    response = call(client, "PUT", "/settings", key, json={"page_protection_enabled": False})
    assert response.status_code == 403 and "page_protection_enabled" in response.json["refused"]
    response = client.post("/admin/plugins/page_governance/disable", data={"force": "1"}, follow_redirects=True)
    assert b"unlock delay" in response.data

    assert db.scalar("SELECT page_protection_enabled FROM site_settings") == 1
    assert db.scalar("SELECT enabled FROM plugins WHERE id = 'page_governance'") == 1
    client.post(f"/page/{page['slug']}/edit", data={"content": "admin text", "revision": 1})
    assert get_page(app, page["id"])["content"] == "text"
    assert _switches(db) == []


def test_hw1_3_the_controller_may_switch_protection_off_and_every_switch_is_audited(app, client, login,
                                                                                   make_user, db):
    from tests.features.api_support import call, enable_api, issue

    controller = make_user("controlleradmin", role="admin")
    _protected_page(app, db, controller)
    login(client, controller)

    assert client.post("/admin/governance", data=_NUMBERS).status_code == 302
    assert db.scalar("SELECT page_protection_enabled FROM site_settings") == 0
    enable_api(app, db)
    key = issue(app, controller, ["settings"], name="settings")
    assert call(client, "PUT", "/settings", key, json={"page_protection_enabled": True}).status_code == 200

    assert _switches(db) == ['{"enabled": false}', '{"enabled": true}']
