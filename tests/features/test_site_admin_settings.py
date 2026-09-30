"""Site administration: general settings, access rules and the public-mode notice."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in

PAGES = ["/admin/settings", "/admin/appearance", "/admin/interface-languages", "/admin/documentation",
         "/admin/migration", "/admin/server", "/admin/error-log", "/admin/audit"]
POSTS = ["/admin/settings", "/global-settings", "/admin/appearance/colors", "/admin/appearance/reset",
         "/admin/settings/favicon/select", "/admin/interface-languages/upload", "/admin/spawn-docs",
         "/admin/docs-settings", "/admin/migration/export", "/admin/migration/import",
         "/admin/settings/restart-server", "/global-settings/restart-server", "/admin/audit/retention"]


def valid_form(**overrides):
    form = {
        "site_name": "Team Wiki", "timezone": "Europe/Rome", "approval_pending_timeout_hours": "0",
        "approval_denied_timeout_hours": "24", "maintenance_message": "", "public_mode_message": "",
        "upload_mode": "blacklist", "upload_whitelist": "", "upload_blacklist": "exe, .BAT",
        "upload_max_size_mb": "50", "upload_quota_per_day_count": "10", "upload_quota_per_day_bytes": "5",
        "draft_expiration_hours": "0", "intro_role_switching_roles": ["user", "admin"], "pdf_export_enabled": "1",
    }
    form.update(overrides)
    return form


def settings_row(db):
    return db.one("SELECT * FROM site_settings WHERE id = 1")


@pytest.mark.parametrize("role", ["user", "editor"])
def test_every_page_and_action_is_admin_only(client, make_user, login, db, role):
    login(client, make_user("someone", role=role))
    for url in PAGES:
        assert client.get(url).status_code == 403, url
    for url in POSTS:
        assert client.post(url, data={}).status_code == 403, url
    assert settings_row(db)["site_name"] == "BananaWiki"


def test_anonymous_visitors_are_sent_to_sign_in(client):
    assert client.get("/admin/settings").status_code == 302
    assert client.post("/admin/settings", data=valid_form()).status_code == 302


def test_legacy_url_redirects_and_accepts_posts(admin_client, db):
    assert admin_client.get("/global-settings").headers["Location"].endswith("/admin/settings")
    assert admin_client.post("/global-settings", data=valid_form(site_name="Legacy")).status_code == 302
    assert settings_row(db)["site_name"] == "Legacy"


def test_saving_stores_normalised_values(admin_client, db):
    response = admin_client.post("/admin/settings", data=valid_form())
    assert response.status_code == 302
    row = settings_row(db)
    assert row["site_name"] == "Team Wiki" and row["timezone"] == "Europe/Rome"
    assert row["upload_blacklist"] == "exe,bat"
    assert row["upload_quota_per_day_bytes"] == 5 * 1024 * 1024
    assert row["intro_role_switching_roles"] == "user,admin"
    assert row["pdf_export_enabled"] == 1 and row["markdown_export_enabled"] == 0
    assert row["public_mode"] == 0 and row["public_mode_until"] is None
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'settings.updated'") == 1


def test_empty_site_name_falls_back(admin_client, db):
    admin_client.post("/admin/settings", data=valid_form(site_name="  "))
    assert settings_row(db)["site_name"] == "BananaWiki"


@pytest.mark.parametrize("field, value", [
    ("site_name", "x" * 101),
    ("timezone", "Mars/Olympus"),
    ("upload_mode", "anything"),
    ("upload_max_size_mb", "0"),
    ("approval_denied_timeout_hours", "abc"),
    ("upload_blacklist", "exe, ../etc"),
    ("intro_role_switching_roles", ["owner"]),
])
def test_invalid_values_change_nothing(admin_client, db, field, value):
    response = admin_client.post("/admin/settings", data=valid_form(**{field: value}))
    assert response.status_code == 400
    assert settings_row(db)["site_name"] == "BananaWiki"


def test_time_limits_must_lie_in_the_future(admin_client, db):
    past = admin_client.post("/admin/settings", data=valid_form(public_mode="1", public_mode_until="2000-01-01T10:00"))
    assert past.status_code == 400
    admin_client.post("/admin/settings", data=valid_form(open_signup="1", open_signup_until="2999-01-01T10:00"))
    row = settings_row(db)
    assert row["open_signup"] == 1 and row["open_signup_until"] == "2999-01-01 10:00:00"


def test_expired_public_mode_is_shown_as_off(admin_client, db):
    db.execute("UPDATE site_settings SET public_mode = 1, public_mode_until = ? WHERE id = 1", (sql_in(hours=-1),))
    page = admin_client.get("/admin/settings").get_data(as_text=True)
    assert 'name="public_mode" value="1" checked' not in page


def test_host_locks_are_shown_and_respected(app_factory, login):
    app = app_factory(environ={"BW_FORBID_PUBLIC_MODE": "1", "BW_FORBID_PAGE_BUILDER": "1",
                               "BW_MANAGED_HOSTING": "1", "BW_PLATFORM_UPLOAD_BLACKLIST": "php,sh"})
    client = app.test_client()
    from bananawiki.wiki import accounts
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        admin = accounts.create("boss", "correct horse battery", role="admin", emit_event=False)
    login(client, admin)
    page = client.get("/admin/settings").get_data(as_text=True)
    assert "does not allow public access" in page and "php, sh" in page
    client.post("/admin/settings", data=valid_form(public_mode="1", page_builder_enabled="1",
                                                    upload_max_size_mb="2000"))
    from bananawiki.core.sqlite import Session

    session = Session(app.extensions["bananawiki.database"].connect())
    row = session.one("SELECT * FROM site_settings WHERE id = 1")
    session.conn.close()
    assert row["public_mode"] == 0 and row["page_builder_enabled"] == 0 and row["upload_max_size_mb"] == 100
    assert row["site_name"] == "Team Wiki"


def test_page_builder_switch(admin_client, db):
    admin_client.post("/admin/settings", data=valid_form(page_builder_enabled="1"))
    assert settings_row(db)["page_builder_enabled"] == 1


def test_public_notice_banner_for_visitors(app, client, db):
    db.execute("UPDATE site_settings SET public_mode = 1, public_mode_show_message = 1, "
               "public_mode_message = '<b>Read only</b>' WHERE id = 1")
    home = client.get("/", follow_redirects=True).get_data(as_text=True)
    assert "&lt;b&gt;Read only&lt;/b&gt;" in home


def test_csrf_is_required(app, admin_client, db):
    app.config["CSRF_DISABLED"] = False
    assert admin_client.post("/admin/settings", data=valid_form()).status_code in (400, 403)
    assert settings_row(db)["site_name"] == "BananaWiki"


def test_translations_have_the_same_keys():
    import json
    from pathlib import Path

    for feature in ("site_admin", "audit"):
        folder = Path(__file__).parents[2] / "bananawiki" / "wiki" / "features" / feature / "translations"
        en = json.loads((folder / "en.json").read_text(encoding="utf-8"))
        it = json.loads((folder / "it.json").read_text(encoding="utf-8"))
        assert en.keys() == it.keys()
