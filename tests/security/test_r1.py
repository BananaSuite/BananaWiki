"""Regression tests for the r1 findings of the full re-audit pass 12 (audit entries for page history,
page governance administration and announcements)."""

from __future__ import annotations

import json

from bananawiki.wiki.db import connection_scope


def _audit_rows(db, start: int) -> list[dict]:
    return [{**row, "details": json.loads(row["details"])} for row in
            db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? ORDER BY id",
                   (start,))]


def _last_audit_id(db) -> int:
    return db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")


# RW2-1: page-history deletion, clearing and re-attribution are audited


def test_rw2_1_history_deletion_and_reattribution_are_audited(app, db, make_user, login):
    from bananawiki.wiki.features.pages import service

    admin = make_user("plainadmin", role="admin")
    editor = make_user("editorone", role="editor")
    make_user("innocent", role="editor")
    with app.test_request_context(), connection_scope():
        page = service.create("Ledger", "v1 by editor", author_id=editor["id"])
        service.update(page, author_id=editor["id"], content="vandalism by editor")
        service.update(service.get(page["id"]), author_id=editor["id"], content="third")
    first, second, last = db.column("SELECT id FROM page_history WHERE page_id = ? ORDER BY id", (page["id"],))
    client = app.test_client()
    login(client, admin)
    start = _last_audit_id(db)
    assert client.post(f"/page/ledger/history/{last}/transfer", data={"username": "innocent"}).status_code == 302
    assert client.post("/page/ledger/history/bulk-transfer",
                       data={"from_username": "editorone", "username": "innocent"}).status_code == 302
    assert client.post(f"/page/ledger/history/{first}/deattribute").status_code == 302
    assert client.post(f"/page/ledger/history/{second}/delete").status_code == 302
    assert client.post("/page/ledger/history/clear").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == 0

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == ["page.history_reattributed", "page.history_reattributed",
                                               "page.history_deattributed", "page.history_entry_deleted",
                                               "page.history_cleared"]
    assert {(row["actor_id"], row["target_type"], row["target_id"]) for row in rows} == \
        {(str(admin["id"]), "page", str(page["id"]))}
    moved, bulk, anonymous, deleted, cleared = (row["details"] for row in rows)
    assert moved == {"title": "Ledger", "entry": last, "editor": "editorone", "to": "innocent"}
    assert bulk == {"title": "Ledger", "editor": "editorone", "to": "innocent", "count": 2}
    assert anonymous == {"title": "Ledger", "entry": first, "editor": "innocent"}
    assert deleted["entry"] == second and deleted["editor"] == "innocent"
    assert cleared["count"] == 2


def test_rw2_1_a_refused_history_change_records_nothing(app, db, make_user, login):
    from bananawiki.wiki.features.pages import service

    with app.test_request_context(), connection_scope():
        page = service.create("Ledger", "v1", author_id=None)
    entry = db.scalar("SELECT id FROM page_history WHERE page_id = ?", (page["id"],))
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))
    start = _last_audit_id(db)
    client.post(f"/page/ledger/history/{entry}/transfer", data={"username": "nobody-here"})
    client.post("/page/ledger/history/bulk-transfer", data={"from_username": "nobody-here", "username": "plainadmin"})
    assert _audit_rows(db, start) == []


# RW2-2: unlock requests, forced unlocks, check-out releases and assignments and quota changes are audited


def _governance_pages(app, db, make_user, login):
    from bananawiki.wiki import settings
    from bananawiki.wiki.features.pages import service

    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('page_governance', 'audit')")
    with app.test_request_context(), connection_scope():
        settings.update({"page_reservations_enabled": 1, "page_protection_enabled": 1})
        guarded = service.create("Guarded", "x", author_id=None)
        other = service.create("Other", "x", author_id=None)
    controller, holder = make_user("controller", role="editor"), make_user("holderone", role="editor")
    for user, url in ((controller, "/page/guarded/protection"), (holder, "/page/other/reserve")):
        client = app.test_client()
        login(client, user)
        assert client.post(url, data={"action": "protect"}).status_code == 302
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (guarded["id"],)) == controller["id"]
    assert db.scalar("SELECT user_id FROM page_reservations WHERE page_id = ? AND released_at IS NULL",
                     (other["id"],)) == holder["id"]
    return guarded, other, controller, holder


def test_rw2_2_governance_admin_actions_are_audited(app, db, make_user, login):
    guarded, other, controller, holder = _governance_pages(app, db, make_user, login)
    admin = make_user("plainadmin", role="admin")
    client = app.test_client()
    login(client, admin)
    start = _last_audit_id(db)
    assert client.post(f"/admin/governance/protection/{guarded['id']}/request-unlock").status_code == 302
    assert client.post(f"/admin/governance/protection/{guarded['id']}/request-unlock").status_code == 302
    db.execute("UPDATE pages SET protection_unlock_requested_at = datetime('now', '-73 hours') WHERE id = ?",
               (guarded["id"],))
    assert client.post(f"/admin/governance/protection/{guarded['id']}/force-unlock").status_code == 302
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (guarded["id"],)) is None
    assert client.post(f"/admin/checkouts/{other['id']}/release").status_code == 302
    assert client.post(f"/admin/checkouts/{other['id']}/assign", data={"username": "controller"}).status_code == 302
    assert client.post(f"/admin/users/{holder['id']}/reservation-quota",
                       data={"action": "set_quota", "unlimited": "1"}).status_code == 302
    assert db.scalar("SELECT reserved_pages_quota FROM users WHERE id = ?", (holder["id"],)) == -1

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == [
        "page_governance.unlock_requested", "page_governance.protection_removed",
        "page_governance.checkout_released", "page_governance.checkout_assigned", "page_governance.quota_set",
    ], "a repeated unlock request keeps the first one and records nothing"
    assert {row["actor_id"] for row in rows} == {str(admin["id"])}
    requested, removed, released, assigned, quota_set = rows
    assert (requested["target_type"], requested["target_id"]) == ("page", str(guarded["id"]))
    assert requested["details"]["controller"] == "controller"
    assert removed["details"]["controller"] == "controller"
    assert removed["details"]["requested_by"] == "plainadmin"
    assert released["details"] == {"holder": "holderone"}
    assert assigned["details"]["holder"] == "controller" and assigned["details"]["previous_holder"] is None
    assert (quota_set["target_type"], quota_set["target_id"]) == ("user", str(holder["id"]))
    assert quota_set["details"] == {"username": "holderone", "old": None, "new": -1}


def test_rw2_2_an_unchanged_quota_records_nothing(app, db, make_user, login):
    _guarded, _other, _controller, holder = _governance_pages(app, db, make_user, login)
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))
    start = _last_audit_id(db)
    client.post(f"/admin/users/{holder['id']}/reservation-quota", data={"action": "reset_quota"})
    assert _audit_rows(db, start) == []


# RW3-4: announcements created, changed and deleted are audited


def _announcement_form(content: str, **extra: str) -> dict:
    return {"content": content, "color": "red", "text_size": "large", "visibility": "logged_in",
            "audience_mode": "all", **extra}


def test_rw3_4_announcement_changes_are_audited(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('announcements', 'audit')")
    admin = make_user("plainada", role="admin")
    client = app.test_client()
    login(client, admin)
    start = _last_audit_id(db)
    text = "Your session expired, [sign in again here](https://evil.example/login)"
    assert client.post("/admin/announcements/create", data=_announcement_form(text)).status_code == 302
    announcement = db.scalar("SELECT id FROM announcements")
    changed = text + " or <a href='//Other.Example/x'>here</a>"
    assert client.post(f"/admin/announcements/{announcement}/edit",
                       data=_announcement_form(changed, is_active="1")).status_code == 302
    assert client.post(f"/admin/announcements/{announcement}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM announcements") == 0

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == ["announcement.created", "announcement.updated",
                                               "announcement.deleted"]
    assert {(row["actor_id"], row["target_type"], row["target_id"]) for row in rows} == \
        {(str(admin["id"]), "announcement", str(announcement))}
    created, updated, deleted = (row["details"] for row in rows)
    assert created["link_hosts"] == "evil.example"
    assert created["visibility"] == "logged_in" and created["audience_mode"] == "all"
    assert created["excerpt"] == text
    assert updated["link_hosts"] == "evil.example, other.example"
    assert updated["changed"] == "content"
    assert deleted["link_hosts"] == "evil.example, other.example", "the entry keeps what the deleted banner said"


def test_rw3_4_a_refused_announcement_records_nothing(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('announcements', 'audit')")
    client = app.test_client()
    login(client, make_user("plainada", role="admin"))
    start = _last_audit_id(db)
    assert client.post("/admin/announcements/create", data=_announcement_form("")).status_code == 400
    assert _audit_rows(db, start) == []


def test_rw3_4_backslash_and_entity_links_name_their_host(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('announcements', 'audit')")
    client = app.test_client()
    login(client, make_user("plainada", role="admin"))
    start = _last_audit_id(db)
    # Past the excerpt, links a browser follows to other sites although they hold no "//"
    text = ("Notice. " * 40 + r"[sign in](/\evil.example/login) or <a href='https:\\Other.Example/x'>here</a> "
            "or <a href='&#47;&#9;&#47;third.example'>there</a>, [help](/page/help)")
    assert client.post("/admin/announcements/create", data=_announcement_form(text)).status_code == 302
    (created,) = _audit_rows(db, start)
    assert "evil.example" not in created["details"]["excerpt"]
    assert created["details"]["link_hosts"] == "evil.example, other.example, third.example"


def _badge_form(name: str, **extra: str) -> dict:
    return {"name": name, "description": "", "icon": "*", "color": "#ffd700", "enabled": "1", **extra}


def test_rw3_4_badge_administration_is_audited(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('badges', 'audit')")
    admin = make_user("plainada", role="admin")
    member = make_user("someone")
    client = app.test_client()
    login(client, admin)
    start = _last_audit_id(db)
    assert client.post("/admin/badges/create", data=_badge_form("Trusted")).status_code == 302
    badge = db.scalar("SELECT id FROM badge_types WHERE name = 'Trusted'")
    assert client.post(f"/admin/badges/{badge}/edit", data=_badge_form("Moderator")).status_code == 302
    assert client.post(f"/admin/badges/{badge}/award", data={"username": "someone"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/revoke", data={"username": "someone"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/award", data={"username": "someone"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/revoke-all", data={"permanent": "1"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/award", data={"username": "someone"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/edit", data={"action": "delete"}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM badge_types WHERE id = ?", (badge,)) == 0

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == [
        "badge.type_created", "badge.type_updated", "badge.awarded", "badge.revoked", "badge.awarded",
        "badge.revoked_all", "badge.awarded", "badge.type_deleted"]
    assert {(row["actor_id"], row["target_type"], row["target_id"]) for row in rows} == \
        {(str(admin["id"]), "badge", str(badge))}
    created, updated, awarded, revoked, _, revoked_all, _, deleted = (row["details"] for row in rows)
    assert created["name"] == "Trusted" and created["enabled"] == 1
    assert updated["name"] == "Moderator" and updated["previous_name"] == "Trusted" and updated["changed"] == "name"
    assert awarded["user"] == "someone" and awarded["user_id"] == member["id"]
    assert revoked["permanent"] is False and revoked["user"] == "someone"
    assert revoked_all == {"name": "Moderator", "permanent": True, "count": 1}
    assert deleted == {"name": "Moderator", "holders": 1}


def test_rw3_4_a_badge_action_that_changes_nothing_records_nothing(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('badges', 'audit')")
    client = app.test_client()
    login(client, make_user("plainada", role="admin"))
    make_user("someone")
    assert client.post("/admin/badges/create", data=_badge_form("Trusted")).status_code == 302
    badge = db.scalar("SELECT id FROM badge_types WHERE name = 'Trusted'")
    start = _last_audit_id(db)
    assert client.post("/admin/badges/create", data=_badge_form("")).status_code == 400
    assert client.post(f"/admin/badges/{badge}/edit", data=_badge_form("Trusted")).status_code == 302
    assert client.post(f"/admin/badges/{badge}/revoke", data={"username": "someone"}).status_code == 302
    assert client.post(f"/admin/badges/{badge}/revoke-all").status_code == 302
    assert client.post(f"/admin/badges/{badge}/award", data={"username": "nobody"}).status_code == 302
    assert _audit_rows(db, start) == []


def test_rw3_4_profile_field_definitions_are_audited(app, db, make_user, login):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id IN ('user_profiles', 'audit')")
    admin = make_user("plainada", role="admin")
    member = make_user("someone")
    administrator, own = app.test_client(), app.test_client()
    login(administrator, admin)
    login(own, member)
    start = _last_audit_id(db)
    assert administrator.post("/admin/profile-fields", data={"action": "create", "label": "Website",
                                                             "field_type": "text"}).status_code == 302
    field = db.scalar("SELECT id FROM user_profile_fields__definitions")
    own.post("/settings/profile-fields", data={f"value_{field}": "mine", f"visible_{field}": "1"})
    assert administrator.post("/admin/profile-fields", data={"action": "update", "field_id": field,
                                                             "label": "Website", "field_type": "text"}).status_code == 302
    assert administrator.post("/admin/profile-fields", data={"action": "update", "field_id": field,
                                                             "label": "Homepage", "field_type": "url"}).status_code == 302
    assert administrator.post("/admin/profile-fields", data={"action": "update", "field_id": field,
                                                             "label": "", "field_type": "url"}).status_code == 302
    assert administrator.post("/admin/profile-fields",
                              data={"action": "delete", "field_id": field}).status_code == 302

    rows = _audit_rows(db, start)
    assert [row["action"] for row in rows] == ["profile_field.created", "profile_field.updated",
                                               "profile_field.deleted"]
    assert {(row["actor_id"], row["target_type"], row["target_id"]) for row in rows} == \
        {(str(admin["id"]), "profile_field", str(field))}
    created, updated, deleted = (row["details"] for row in rows)
    assert created == {"key": "website", "label": "Website", "field_type": "text"}
    assert updated == {"key": "website", "label": "Homepage", "field_type": "url", "previous_label": "Website",
                       "previous_field_type": "text"}
    assert deleted == {"key": "website", "label": "Homepage", "field_type": "url", "values": 1}
