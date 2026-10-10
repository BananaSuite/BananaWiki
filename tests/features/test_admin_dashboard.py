"""Dashboard, admin layout, analytics counters and audit pages."""

from __future__ import annotations

from bananawiki.core.timeutil import utcnow


def test_dashboard_shows_stats_and_pending(admin_client, make_user):
    make_user("waiting_one", approval_status="pending")
    page = admin_client.get("/admin/dashboard").data.decode()
    assert "waiting_one" in page
    assert "Awaiting approval" in page and "Traffic, last 30 days" in page


def test_admin_menu_lists_admin_nav_items(admin_client):
    page = admin_client.get("/admin/dashboard").data.decode()
    for path in ("/admin/users", "/admin/roles", "/admin/codes", "/admin/sessions"):
        assert f'href="{path}"' in page


def test_dashboard_slot_is_rendered(app, admin_client):
    from markupsafe import Markup

    from bananawiki.wiki.registry import registry

    with app.app_context():
        registry()._slots.setdefault("admin.dashboard", []).append(("admin", lambda: Markup("<p>slot-card</p>")))
    assert b"slot-card" in admin_client.get("/admin/dashboard").data


def test_requests_are_counted(admin_client, db):
    admin_client.get("/_probe/private")
    admin_client.get("/_probe/private")
    today = utcnow().strftime("%Y-%m-%d")
    count = db.scalar("SELECT count FROM analytics_daily WHERE day = ? AND kind = 'request'", (today,))
    assert count >= 2
    assert admin_client.get("/health").status_code == 200
    assert db.scalar("SELECT count FROM analytics_daily WHERE day = ? AND kind = 'request'", (today,)) == count


def test_analytics_summary_fills_gaps(app, db):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.admin import analytics

    db.execute("INSERT INTO analytics_daily (day, kind, count) VALUES (?, 'page_view', 7)",
               (utcnow().strftime("%Y-%m-%d"),))
    with app.app_context(), connection_scope():
        data = analytics.summary(7)
    assert len(data["daily"]) == 7 and data["totals"]["page_view"] == 7


def test_audit_page_and_attributions(admin_client, admin, make_user, db):
    bob, carol = make_user("bob"), make_user("carol")
    page_id = db.scalar("SELECT id FROM pages LIMIT 1")
    db.execute("INSERT INTO page_history (page_id, title, content, edited_by) VALUES (?, 't', 'c', ?)",
               (page_id, bob["id"]))
    admin_client.post(f"/admin/users/{bob['id']}/edit", data={"action": "suspend", "suspend_reason": "rude"})
    page = admin_client.get(f"/admin/users/{bob['id']}/audit").data.decode()
    assert "rude" in page and "1 page edit(s)" in page
    admin_client.post(f"/admin/users/{bob['id']}/attributions",
                      data={"action": "mass_reattribute", "to_username": "carol"})
    assert db.scalar("SELECT edited_by FROM page_history WHERE page_id = ? AND title = 't'", (page_id,)) == carol["id"]
    admin_client.post(f"/admin/users/{carol['id']}/attributions", data={"action": "deattribute_all"})
    assert db.scalar("SELECT edited_by FROM page_history WHERE title = 't'") is None


def test_role_history_entries_can_be_deleted(admin_client, make_user, db):
    bob, carol = make_user("bob"), make_user("carol")
    admin_client.post(f"/admin/users/{bob['id']}/edit", data={"action": "change_role", "role": "editor"})
    admin_client.post(f"/admin/users/{carol['id']}/edit", data={"action": "change_role", "role": "editor"})
    carol_entry = db.scalar("SELECT id FROM role_history WHERE user_id = ?", (carol["id"],))
    admin_client.post(f"/admin/users/{bob['id']}/attributions",
                      data={"action": "delete_role_history_entry", "entry_id": str(carol_entry)})
    assert db.scalar("SELECT COUNT(*) FROM role_history WHERE user_id = ?", (carol["id"],)) == 1
    admin_client.post(f"/admin/users/{bob['id']}/attributions", data={"action": "delete_all_role_history"})
    assert db.scalar("SELECT COUNT(*) FROM role_history WHERE user_id = ?", (bob["id"],)) == 0


def test_pages_render_in_italian(admin_client, make_user):
    bob = make_user("bob", role="editor")
    for url in ("/admin/dashboard", "/admin/users", f"/admin/users/{bob['id']}", "/admin/roles/create",
                "/admin/codes", "/admin/sessions", f"/admin/users/{bob['id']}/permissions"):
        response = admin_client.get(url + "?lang=it")
        assert response.status_code == 200
    assert "Utenti" in admin_client.get("/admin/users?lang=it").data.decode()


def test_translations_have_same_keys():
    import json
    from pathlib import Path

    import bananawiki.wiki.features.admin as feature
    from bananawiki.wiki.i18n import BUILTIN_LANGUAGES

    folder = Path(feature.__file__).parent / "translations"
    en = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    for language in sorted(set(BUILTIN_LANGUAGES) - {"en"}):
        translated = json.loads((folder / f"{language}.json").read_text(encoding="utf-8"))
        assert en.keys() == translated.keys(), language
