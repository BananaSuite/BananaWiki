"""Built-in documentation, server restart and the error log."""

from __future__ import annotations

import io
import os
import signal
import zipfile

from bananawiki.core.timeutil import now_sql

GUNICORN = {"SERVER_SOFTWARE": "gunicorn/23.0.0"}


def test_spawn_docs_creates_the_category(admin_client, db):
    assert admin_client.post("/admin/spawn-docs", data={"simplified": "1", "docs_language": "it"}).status_code == 302
    category_id = db.scalar("SELECT docs_category_id FROM site_settings")
    assert category_id and db.scalar("SELECT COUNT(*) FROM pages WHERE category_id = ?", (category_id,)) > 0
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'docs.spawned'") == 1
    admin_client.post("/admin/spawn-docs", data={"variant": "full"})
    assert db.scalar("SELECT docs_category_id FROM site_settings") != category_id


def test_documentation_page_offers_every_builtin_language(admin_client):
    from bananawiki.wiki.features.auth import docs

    page = admin_client.get("/admin/documentation").get_data(as_text=True)
    assert all(f'<option value="{code}"' in page for code in docs.LANGUAGES)
    assert "site_admin.docs.language." not in page  # a language without a label of its own shows its name
    assert ">German<" in page  # each guide language is named in the interface language


def test_download_docs_is_a_zip_of_markdown(admin_client):
    response = admin_client.get("/admin/download-docs?docs_language=en&variant=simplified")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
    assert names and all(name.startswith("BananaWiki/") and name.endswith(".md") for name in names)


def test_docs_settings_need_deletion_slowdown(app, admin_client, db):
    from bananawiki.wiki import registry
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        registry.set_enabled("deletion_slowdown", True)
    admin_client.post("/admin/docs-settings", data={})
    assert db.scalar("SELECT docs_bypass_deletion_slowdown FROM site_settings") == 0
    with app.test_request_context(), connection_scope():
        registry.set_enabled("deletion_slowdown", False)
    admin_client.post("/admin/docs-settings", data={"docs_bypass_deletion_slowdown": "1"})
    assert db.scalar("SELECT docs_bypass_deletion_slowdown FROM site_settings") == 0


def test_restart_explains_outside_gunicorn(admin_client, monkeypatch, db):
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    page = admin_client.post("/admin/settings/restart-server", data={"confirm": "1"}, follow_redirects=True)
    assert "not running under Gunicorn" in page.get_data(as_text=True)
    assert sent == [] and db.scalar("SELECT last_server_restart_at FROM site_settings") is None
    assert admin_client.get("/admin/settings/restart-server").status_code == 405


def test_restart_signals_gunicorn_once_per_cooldown(admin_client, monkeypatch, db):
    from bananawiki.wiki.features.site_admin import server

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(server, "gunicorn_master_pid", lambda: 4242)
    admin_client.post("/global-settings/restart-server", environ_base=GUNICORN)
    assert sent == []  # not confirmed
    admin_client.post("/global-settings/restart-server", data={"confirm": "1"}, environ_base=GUNICORN)
    assert sent == [(4242, signal.SIGHUP)]
    page = admin_client.post("/admin/server/restart", data={"confirm": "1"}, environ_base=GUNICORN,
                             follow_redirects=True)
    assert len(sent) == 1 and "restarted recently" in page.get_data(as_text=True)
    db.execute("UPDATE site_settings SET last_server_restart_at = '2020-01-01T00:00:00+00:00'")
    admin_client.post("/admin/server/restart", data={"confirm": "1"}, environ_base=GUNICORN)
    assert len(sent) == 2 and db.scalar("SELECT last_server_restart_at FROM site_settings") <= now_sql()


def test_gunicorn_detection_checks_the_parent(app):
    from bananawiki.wiki.features.site_admin import server

    with app.test_request_context(environ_base=GUNICORN):
        assert server.gunicorn_master_pid() == 0  # pytest's parent is not gunicorn
    with app.test_request_context():
        assert server.gunicorn_master_pid() == 0


def test_error_log_is_redacted(app, admin_client):
    cfg = app.config["BW"]
    os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
    with open(cfg.log_file, "w", encoding="utf-8") as handle:
        handle.write("2026-01-01 10:00:00,000 INFO [bananawiki] started\n")
        handle.write(f"2026-01-01 10:00:01,000 ERROR [bananawiki] boom key={cfg.secret_key} password=hunter2\n")
        handle.write("Traceback: Authorization: Bearer abc.def <script>\n")
    errors = admin_client.get("/admin/error-log").get_data(as_text=True)
    assert "boom" in errors and "started" not in errors
    assert cfg.secret_key not in errors and "hunter2" not in errors and "abc.def" not in errors
    assert "<script>" not in errors
    assert "started" in admin_client.get("/admin/error-log?level=all").get_data(as_text=True)


def test_missing_log_is_explained(admin_client):
    assert "No log file" in admin_client.get("/admin/error-log").get_data(as_text=True)
