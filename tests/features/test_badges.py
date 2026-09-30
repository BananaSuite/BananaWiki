"""Badges: administration, automatic triggers, notifications and reading time."""

from __future__ import annotations

from .people_support import edit_page, in_app, make_category, make_page, publish, set_feature

BADGE = {"name": "Helper", "description": "Helps", "icon": "*", "color": "#123456", "enabled": "1"}


def _create(admin_client, db, **extra) -> int:
    admin_client.post("/admin/badges/create", data={**BADGE, **extra})
    return db.scalar("SELECT id FROM badge_types WHERE name = ?", (extra.get("name", BADGE["name"]),))


def test_admin_creates_edits_and_deletes(admin_client, db):
    badge_id = _create(admin_client, db)
    assert db.scalar("SELECT color FROM badge_types WHERE id = ?", (badge_id,)) == "#123456"
    assert admin_client.get(f"/admin/badges/{badge_id}/edit").status_code == 200
    admin_client.post(f"/admin/badges/{badge_id}/edit", data={**BADGE, "name": "Great helper"})
    assert db.scalar("SELECT name FROM badge_types WHERE id = ?", (badge_id,)) == "Great helper"
    admin_client.post(f"/admin/badges/{badge_id}/edit", data={"action": "delete"})
    assert db.scalar("SELECT COUNT(*) FROM badge_types") == 0


def test_badge_validation(admin_client, db):
    assert admin_client.post("/admin/badges/create", data={**BADGE, "color": "red"}).status_code == 400
    assert admin_client.post("/admin/badges/create", data={**BADGE, "trigger_type": "easter_egg"}).status_code == 400
    assert admin_client.post("/admin/badges/create", data={**BADGE, "auto_trigger": "1"}).status_code == 400
    _create(admin_client, db)
    assert admin_client.post("/admin/badges/create", data=BADGE).status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM badge_types") == 1


def test_badge_admin_is_admin_only(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/admin/badges").status_code == 403
    assert client.post("/admin/badges/create", data=BADGE).status_code == 403


def test_default_badges_are_added_once_and_disabled(admin_client, db):
    admin_client.post("/admin/badges/defaults")
    admin_client.post("/admin/badges/defaults")
    assert db.scalar("SELECT COUNT(*) FROM badge_types") == 6
    assert db.scalar("SELECT COUNT(*) FROM badge_types WHERE enabled = 1") == 0


def test_manual_award_revoke_and_notification(app, admin_client, make_user, login, db):
    badge_id = _create(admin_client, db)
    bob = make_user("bob")
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (bob["id"],)) == 1
    client = app.test_client()
    login(client, bob)
    page = client.get("/settings").get_data(as_text=True)
    assert "You earned new badges: 1" in page
    assert b"Helper" in client.get("/badges/notifications").data
    client.post("/badges/notifications/dismiss")
    assert db.scalar("SELECT COUNT(*) FROM badge_notifications WHERE notified = 0") == 0
    assert b"Helper" in client.get("/users/bob").data
    admin_client.post(f"/admin/badges/{badge_id}/revoke", data={"username": "bob"})
    assert db.scalar("SELECT revoked FROM user_badges WHERE user_id = ?", (bob["id"],)) == 1
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ? AND revoked = 0", (bob["id"],)) == 1
    admin_client.post(f"/admin/badges/{badge_id}/revoke-all", data={"permanent": "1"})
    assert db.scalar("SELECT COUNT(*) FROM user_badges") == 0


def test_multiple_awards_when_allowed(admin_client, make_user, db):
    badge_id = _create(admin_client, db, allow_multiple="1")
    make_user("bob")
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    assert db.scalar("SELECT COUNT(*) FROM user_badges") == 2
    assert db.scalar("SELECT COUNT(*) FROM badge_notifications WHERE notified = 0") == 1


def test_first_edit_and_count_triggers_fire_on_save(app, admin_client, make_user, db):
    _create(admin_client, db, name="First", auto_trigger="1", trigger_type="first_edit")
    _create(admin_client, db, name="Two edits", auto_trigger="1", trigger_type="contribution_count",
            trigger_threshold="2", allow_multiple="1")
    bob = make_user("bob", role="editor")
    page = make_page(app, "Doc", "x", author_id=bob["id"])
    names = db.column("SELECT bt.name FROM user_badges ub JOIN badge_types bt ON bt.id = ub.badge_type_id "
                      "WHERE ub.user_id = ?", (bob["id"],))
    assert names == ["First"]
    edit_page(app, page["id"], "y", author_id=bob["id"])
    edit_page(app, page["id"], "z", author_id=bob["id"])
    rows = db.all("SELECT bt.name, COUNT(*) AS n FROM user_badges ub JOIN badge_types bt "
                  "ON bt.id = ub.badge_type_id WHERE ub.user_id = ? GROUP BY bt.name", (bob["id"],))
    counts = {row["name"]: row["n"] for row in rows}
    assert counts == {"First": 1, "Two edits": 1}


def test_job_evaluates_category_article_and_membership_triggers(app, admin_client, make_user, db):
    from bananawiki.wiki.features.badges import service

    _create(admin_client, db, name="Two categories", auto_trigger="1", trigger_type="category_count",
            trigger_threshold="2")
    _create(admin_client, db, name="Author", auto_trigger="1", trigger_type="article_count", trigger_threshold="2")
    _create(admin_client, db, name="Veteran", auto_trigger="1", trigger_type="member_days", trigger_threshold="30")
    bob = make_user("bob", role="editor", created_at="2000-01-01 00:00:00")
    carol = make_user("carol", role="editor")
    first, second = make_category(app, "One"), make_category(app, "Two")
    db.execute("UPDATE badge_types SET enabled = 0")
    make_page(app, "A", "x", author_id=bob["id"], category_id=first["id"])
    page_b = make_page(app, "B", "x", author_id=carol["id"], category_id=second["id"])
    edit_page(app, page_b["id"], "y", author_id=bob["id"])
    db.execute("UPDATE badge_types SET enabled = 1")
    in_app(app, service.evaluate_everyone)
    bob_badges = set(db.column("SELECT bt.name FROM user_badges ub JOIN badge_types bt ON bt.id = ub.badge_type_id "
                               "WHERE ub.user_id = ?", (bob["id"],)))
    assert bob_badges == {"Two categories", "Veteran"}
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (carol["id"],)) == 0
    assert in_app(app, service.evaluate_everyone) == 0


def test_reading_time_heartbeat(app, admin_client, make_user, login, db):
    from bananawiki.wiki.features.badges import service

    _create(admin_client, db, name="Reader", auto_trigger="1", trigger_type="reading_time", trigger_threshold="1")
    bob = make_user("bob")
    client = app.test_client()
    login(client, bob)
    assert client.post("/badges/reading", json={"seconds": 5000}).json["ok"]
    assert client.post("/badges/reading", json={"seconds": 60}).status_code == 429
    assert db.scalar("SELECT seconds FROM badge_reading_time WHERE user_id = ?", (bob["id"],)) == 120
    in_app(app, service.evaluate_everyone)
    assert db.scalar("SELECT COUNT(*) FROM user_badges WHERE user_id = ?", (bob["id"],)) == 1


def test_disabled_badges_feature(app, admin_client, make_user, db):
    badge_id = _create(admin_client, db)
    bob = make_user("bob")
    admin_client.post(f"/admin/badges/{badge_id}/award", data={"username": "bob"})
    publish(db, bob)
    set_feature(app, "badges", False)
    assert admin_client.get("/admin/badges").status_code == 404
    assert b"Helper" not in admin_client.get("/users/bob").data
