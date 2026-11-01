"""Regression tests for the QW1 findings of the full re-audit pass 11 (API token churn, plugin manager and
migration password checks, replacing the built-in guide)."""

from __future__ import annotations

import io

from tests.features.api_support import call, enable_api, issue
from tests.features.pages_support import in_app

# QW1-1: API tokens and userbot keys created and revoked in a loop


def _rows(db, user):
    return db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE user_id = ?", (user["id"],))


def test_qw1_1_token_creations_are_capped_per_day(app, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.api_service import tokens

    monkeypatch.setattr(tokens, "MAX_CREATED_PER_DAY", 6)
    db.execute("UPDATE site_settings SET api_service_max_tokens_per_user = 3 WHERE id = 1")
    enable_api(app, db)
    user = make_user("looper", role="user", api_access_enabled=1)
    client = app.test_client()
    login(client, user)
    for _ in range(4):
        assert client.post("/settings/api-tokens/create", data={"name": "t", "scopes": ["pages"]}).status_code == 201
        token_id = db.scalar("SELECT MAX(id) FROM api_service__tokens WHERE user_id = ?", (user["id"],))
        assert client.post(f"/settings/api-tokens/{token_id}/revoke").status_code == 302
    for _ in range(2):
        assert client.post("/settings/api-tokens/userbot", data={"enable": "1"}).status_code == 200
    assert _rows(db, user) == 6
    refused = client.post("/settings/api-tokens/create", data={"name": "t", "scopes": ["pages"]})
    assert refused.status_code == 302
    refused = client.post("/settings/api-tokens/userbot", data={"enable": "1"}, follow_redirects=True)
    assert "in the last 24 hours" in refused.get_data(as_text=True)
    assert _rows(db, user) == 6
    # The refused re-issue kept the current userbot key.
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE user_id = ? AND name = 'userbot' "
                     "AND active = 1", (user["id"],)) == 1
    # Through the API as well; a day later the budget is back.
    db.execute("UPDATE api_service__tokens SET created_at = '2000-01-01 00:00:00' WHERE user_id = ?", (user["id"],))
    parent = issue(app, user, scopes=("tokens", "pages"))
    api = app.test_client()
    for _ in range(6):  # the parent and five children fill the day's six
        created = call(api, "POST", "/tokens", parent, json={"name": "c", "permissions": {"read": True,
                                                                                       "scopes": ["pages"]}})
        if created.status_code != 201:
            break
        assert call(api, "DELETE", f"/tokens/{created.get_json()['id']}", parent).status_code == 200
    assert created.status_code == 429 and created.get_json()["code"] == "too_many_tokens_today"


def test_qw1_1_old_revoked_tokens_are_pruned(app, make_user, db):
    from bananawiki.wiki.features.api_service import _prune, tokens

    enable_api(app, db)
    user = make_user("old", role="user", api_access_enabled=1)
    for name in ("revoked", "expired", "live", "recent"):
        issue(app, user, name=name)
    ids = {row["name"]: row["id"] for row in db.all("SELECT name, id FROM api_service__tokens WHERE user_id = ?",
                                                     (user["id"],))}
    child = in_app(app, lambda: tokens.create(user["id"], name="child", grant=tokens.Grant(True, False, ("pages",)),
                                              parent_id=ids["revoked"])[1])
    in_app(app, lambda: tokens.revoke(ids["revoked"]))
    db.execute("UPDATE api_service__tokens SET created_at = '2000-01-01 00:00:00' WHERE name IN ('revoked', 'live')")
    db.execute("UPDATE api_service__tokens SET expires_at = '2000-06-01 00:00:00' WHERE name = 'expired'")
    db.execute("UPDATE api_service__tokens SET active = 0 WHERE name = 'recent'")
    in_app(app, _prune)
    left = db.column("SELECT name FROM api_service__tokens WHERE user_id = ? ORDER BY name", (user["id"],))
    assert left == ["live", "recent"]
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens WHERE id = ?", (child,)) == 0


# QW1-2: the plugin manager and the site migration share the account's password budget

PASSWORD = "correct horse battery"


def _spend_password_budget(client, db):
    from bananawiki.wiki.features.users.service import PASSWORD_CHECKS

    for attempt in range(PASSWORD_CHECKS):
        client.post("/settings/username", data={"new_username": "renamed", "password": f"wrong{attempt}"})
    assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'account:password'") == PASSWORD_CHECKS


def test_qw1_2_plugin_enable_refuses_the_right_password_once_the_budget_is_spent(app_factory, tmp_path, make_user,
                                                                                  login, db):
    from tests.features.plugins_support import HELLO_PLUGIN, install_folder, plugins_dir, row

    install_folder(tmp_path, HELLO_PLUGIN)
    app = app_factory(environ={"BW_EXTERNAL_PLUGINS_DIR": str(plugins_dir(tmp_path)),
                               "BW_ALLOW_EXTERNAL_PLUGINS": "1"})
    client = app.test_client()
    login(client, make_user("chief", role="owner"))
    _spend_password_budget(client, db)
    refused = client.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD})
    assert refused.status_code == 200 and "Too many" in refused.get_data(as_text=True)
    assert row(app, "hello_plugin")["enabled"] == 0
    upload = client.post("/admin/plugins/import", data={"password": PASSWORD, "plugin_file": (io.BytesIO(b"junk"),
                                                                                              "x.bwplugin")},
                         content_type="multipart/form-data", follow_redirects=True)
    assert "not a valid" not in upload.get_data(as_text=True).lower()
    assert "Too many" in upload.get_data(as_text=True)


def test_qw1_2_plugin_password_guesses_spend_the_shared_budget(app_factory, tmp_path, make_user, login, db):
    from bananawiki.wiki.features.users.service import PASSWORD_CHECKS
    from tests.features.plugins_support import HELLO_PLUGIN, install_folder, plugins_dir

    install_folder(tmp_path, HELLO_PLUGIN)
    app = app_factory(environ={"BW_EXTERNAL_PLUGINS_DIR": str(plugins_dir(tmp_path)),
                               "BW_ALLOW_EXTERNAL_PLUGINS": "1"})
    client = app.test_client()
    login(client, make_user("chief", role="owner"))
    for attempt in range(3):
        client.post("/admin/plugins/hello_plugin/enable", data={"password": f"guess{attempt}"})
    assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'account:password'") == 3
    # The right password is not counted, and still works within the budget.
    assert client.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'account:password'") == 3 < PASSWORD_CHECKS


def test_qw1_2_site_export_refuses_the_right_password_once_the_budget_is_spent(client, make_user, login, db):
    login(client, make_user("chief", role="owner"))
    _spend_password_budget(client, db)
    refused = client.post("/admin/migration/export", data={"password": PASSWORD}, follow_redirects=True)
    assert refused.mimetype == "text/html" and "Too many" in refused.get_data(as_text=True)


# QW1-3: replacing the built-in guide deletes only pages the installer wrote and the administrator could change


def _guide_with_editor(app, make_user, login, db):
    from tests.features.pages_support import set_feature

    for feature in ("page_governance", "deletion_slowdown"):
        set_feature(app, feature, True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1")
    owner = app.test_client()
    login(owner, make_user("theowner", role="owner"))
    assert owner.post("/admin/spawn-docs", data={"variant": "full", "docs_language": "en"}).status_code == 302
    editor = app.test_client()
    login(editor, make_user("editorone", role="editor"))
    admin = app.test_client()
    login(admin, make_user("plainadmin", role="admin"))
    return db.scalar("SELECT docs_category_id FROM site_settings"), editor, admin


def _replace(admin):
    response = admin.post("/admin/spawn-docs", data={"variant": "simplified", "docs_language": "en"},
                          follow_redirects=True)
    return response.get_data(as_text=True)


def test_qw1_3_replacing_the_guide_keeps_a_protected_page_people_wrote(app, make_user, login, db):
    category, editor, admin = _guide_with_editor(app, make_user, login, db)
    assert editor.post("/create", data={"title": "Team runbook", "content": "keep me",
                                        "category_id": str(category)}).status_code == 302
    page = db.one("SELECT * FROM pages WHERE title = 'Team runbook'")
    editor.post(f"/page/{page['slug']}/protection", data={"action": "protect"})
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (page["id"],))
    guide_pages = db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category,))
    assert "Team runbook" in _replace(admin)
    assert db.one("SELECT content FROM pages WHERE id = ?", (page["id"],))["content"] == "keep me"
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) >= 1
    # Nothing changed: the old guide and its pages are all still there.
    assert db.scalar("SELECT docs_category_id FROM site_settings") == category
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category,)) == guide_pages


def test_qw1_3_replacing_the_guide_keeps_an_unprotected_page_people_wrote(app, make_user, login, db):
    category, editor, admin = _guide_with_editor(app, make_user, login, db)
    assert editor.post("/create", data={"title": "Our notes", "content": "mine",
                                        "category_id": str(category)}).status_code == 302
    assert "Our notes" in _replace(admin)
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE title = 'Our notes' AND pending_deletion = 0") == 1
    assert db.scalar("SELECT docs_category_id FROM site_settings") == category


def test_qw1_3_replacing_the_guide_keeps_a_protected_guide_page(app, make_user, login, db):
    category, editor, admin = _guide_with_editor(app, make_user, login, db)
    page = db.one("SELECT p.* FROM pages p JOIN builtin_doc_pages b ON b.page_id = p.id WHERE p.category_id = ? "
                  "ORDER BY p.id LIMIT 1", (category,))
    editor.post(f"/page/{page['slug']}/protection", data={"action": "protect"})
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (page["id"],))
    _replace(admin)
    assert db.one("SELECT id FROM pages WHERE id = ?", (page["id"],)) is not None
    assert db.scalar("SELECT docs_category_id FROM site_settings") == category


def test_qw1_3_replacing_an_untouched_guide_still_works(app, make_user, login, db):
    category, _editor, admin = _guide_with_editor(app, make_user, login, db)
    old = db.column("SELECT id FROM pages WHERE category_id = ?", (category,))
    _replace(admin)
    new_category = db.scalar("SELECT docs_category_id FROM site_settings")
    assert new_category != category
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE id = ?", (category,)) == 0
    assert db.scalar(f"SELECT COUNT(*) FROM pages WHERE id IN ({','.join('?' * len(old))})", tuple(old)) == 0
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (new_category,)) > 0
