"""Admin plugin pages: access, switching features, locks, retired rows and the sidebar order."""

from __future__ import annotations

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope

from .plugins_support import add_feature, build, row


def _enabled(app, feature_id: str) -> bool:
    with app.test_request_context(), connection_scope():
        return registry.is_enabled(feature_id)


def _settings(app) -> dict:
    with app.app_context(), connection_scope() as session:
        return session.one("SELECT * FROM site_settings WHERE id = 1")


def test_pages_are_admin_only(app, client, make_user, login):
    assert client.get("/admin/plugins").status_code == 302
    add_feature(app, "demo")
    login(client, make_user("editor1", role="editor"))
    assert client.get("/admin/plugins").status_code == 403
    assert client.get("/admin/plugins/demo").status_code == 403
    assert client.post("/admin/plugins/demo/disable").status_code == 403
    assert client.post("/admin/plugins/order", data={"order": ["demo"]}).status_code == 403
    assert client.post("/admin/plugins/restart").status_code == 403
    assert _enabled(app, "demo")


def test_list_shows_every_feature_with_its_switch(app, admin_client):
    add_feature(app, "demo")
    page = admin_client.get("/admin/plugins")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    for feature in app.extensions["bananawiki.registry"].ordered():
        assert f"/admin/plugins/{feature.id}\"" in text
    assert "Always on" in text
    assert admin_client.get("/admin/plugins/demo").status_code == 200
    assert admin_client.get("/admin/plugins/does-not-exist").status_code == 404


def test_disabling_keeps_settings_and_enabling_restores(app, admin_client):
    add_feature(app, "demo")
    before = _settings(app)
    response = admin_client.post("/admin/plugins/demo/disable")
    assert response.status_code == 302
    assert not _enabled(app, "demo")
    assert row(app, "demo")["enabled"] == 0
    assert _settings(app) == before  # 1.4 wiped a feature's settings on disable
    admin_client.post("/admin/plugins/demo/enable")
    assert _enabled(app, "demo")
    assert _settings(app) == before


def test_setting_toggle_uses_its_column(app, admin_client):
    add_feature(app, "demo_setting", toggle="setting", setting="pdf_export_enabled")
    admin_client.post("/admin/plugins/demo_setting/disable")
    assert _settings(app)["pdf_export_enabled"] == 0
    assert not _enabled(app, "demo_setting")


def test_always_on_feature_cannot_be_switched_off(app, admin_client):
    admin_client.post("/admin/plugins/plugin_manager/disable")
    assert _enabled(app, "plugin_manager")
    assert admin_client.get("/admin/plugins").status_code == 200


def test_built_in_feature_cannot_be_deleted(app, admin_client):
    add_feature(app, "demo")
    admin_client.post("/admin/plugins/demo/delete")
    assert row(app, "demo") is not None


def test_easy_wiki_hides_and_locks_advanced_features(app_factory, tmp_path, admin, login):
    app = build(app_factory, tmp_path, BW_EASY_WIKI="1")
    add_feature(app, "advanced", easy_wiki=False, default_enabled=False)
    client = app.test_client()
    login(client, admin)
    assert "/admin/plugins/advanced\"" not in client.get("/admin/plugins").get_data(as_text=True)
    assert client.get("/admin/plugins/advanced").status_code == 404
    assert client.post("/admin/plugins/advanced/enable").status_code == 404
    assert row(app, "advanced")["enabled"] == 0


def test_denylisted_feature_is_locked(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path, BW_MANAGED_PLUGIN_DENYLIST="denied")
    add_feature(app, "denied", default_enabled=False)
    client = app.test_client()
    login(client, make_user("boss", role="admin"))
    assert "Not available on this hosting plan" in client.get("/admin/plugins").get_data(as_text=True)
    client.post("/admin/plugins/denied/enable")
    assert row(app, "denied")["enabled"] == 0
    assert not _enabled(app, "denied")


def test_retired_rows_are_inert_and_removable(app_factory, tmp_path, admin, login):
    app = build(app_factory, tmp_path)
    with app.app_context(), connection_scope() as session:
        session.execute("INSERT INTO plugins (id, name, version, builtin, enabled) "
                        "VALUES ('banana_ai', 'Banana AI', '1.0', 1, 1)")
    client = app.test_client()
    login(client, admin)
    text = client.get("/admin/plugins").get_data(as_text=True)
    assert "Retired (inert)" in text and "Banana AI" in text
    assert not _enabled(app, "banana_ai")
    client.post("/admin/plugins/banana_ai/enable")
    assert not _enabled(app, "banana_ai")
    client.post("/admin/plugins/banana_ai/delete")
    assert row(app, "banana_ai") is None


def test_sidebar_order_is_saved_and_moved(app, admin_client):
    from bananawiki.wiki.registry import NavItem

    add_feature(app, "app_a", nav=[NavItem("app_a.nav", "plugin_manager.index", area="apps")])
    add_feature(app, "app_b", nav=[NavItem("app_b.nav", "plugin_manager.index", area="apps")])
    admin_client.post("/admin/plugins/order", data={"order": ["app_b", "app_a", "unknown"]})
    assert _settings(app)["sidebar_apps_order"].startswith("app_b,app_a")
    assert "unknown" not in _settings(app)["sidebar_apps_order"]
    admin_client.post("/admin/plugins/order", data={"order": ["app_b", "app_a"], "move": "app_a:up"})
    assert _settings(app)["sidebar_apps_order"].startswith("app_a,app_b")
    page = admin_client.get("/admin/plugins").get_data(as_text=True)
    assert page.index('data-id="app_a"') < page.index('data-id="app_b"')


def test_state_changes_need_csrf(app, admin_client):
    add_feature(app, "demo")
    app.config["CSRF_DISABLED"] = False
    assert admin_client.post("/admin/plugins/demo/disable").status_code in (400, 403)
    assert _enabled(app, "demo")


def test_restart_explains_when_it_cannot_reload(app, admin_client, monkeypatch):
    import os

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    page = admin_client.post("/admin/plugins/restart", follow_redirects=True)
    assert "process manager" in page.get_data(as_text=True)
    assert sent == []


def test_restart_under_gunicorn_signals_the_master(app, admin_client, monkeypatch):
    import os
    import signal

    from bananawiki.wiki.features.site_admin import server

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(server, "gunicorn_master_pid", lambda: 4242)
    admin_client.post("/admin/plugins/restart", environ_base={"SERVER_SOFTWARE": "gunicorn/23.0.0"})
    assert sent == [(4242, signal.SIGHUP)]
    # The shared one-a-minute cooldown stops a second restart.
    admin_client.post("/admin/plugins/restart", environ_base={"SERVER_SOFTWARE": "gunicorn/23.0.0"})
    assert len(sent) == 1


def test_restart_with_preloaded_app_does_not_signal(app, admin_client, monkeypatch):
    import os

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    app.extensions["bananawiki.plugins"].loaded_pid = os.getpid() + 1
    page = admin_client.post("/admin/plugins/restart", environ_base={"SERVER_SOFTWARE": "gunicorn/23.0.0"},
                             follow_redirects=True)
    assert sent == []
    assert "main process" in page.get_data(as_text=True)


def test_translations_have_the_same_keys():
    import json
    from pathlib import Path

    from bananawiki.wiki.i18n import BUILTIN_LANGUAGES

    folder = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/plugin_manager/translations"
    en = json.loads((folder / "en.json").read_text(encoding="utf-8"))
    for language in sorted(set(BUILTIN_LANGUAGES) - {"en"}):
        translated = json.loads((folder / f"{language}.json").read_text(encoding="utf-8"))
        assert en.keys() == translated.keys(), language

