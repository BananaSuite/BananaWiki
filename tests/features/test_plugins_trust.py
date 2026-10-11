"""Who may bring third-party code into the wiki, and whether it is allowed at all (2.x release audit).

* Installing and enabling plugins is for the owner and superusers; administrators may
  switch plugins off and remove them.
* ``BW_ALLOW_EXTERNAL_PLUGINS`` is off by default for new installations; an existing
  installation that already has plugins keeps them working when the variable is unset.
"""

from __future__ import annotations

import logging

from bananawiki.wiki.db import connection_scope

from .plugins_support import (
    HELLO_PLUGIN,
    archive,
    install_folder,
    plugin_files,
    plugins_dir,
    row,
    set_row,
    upload,
)

PASSWORD = "correct horse battery"


def _build(app_factory, tmp_path, **environ):
    """A wiki whose environment leaves BW_ALLOW_EXTERNAL_PLUGINS unset unless given."""
    return app_factory(environ={"BW_EXTERNAL_PLUGINS_DIR": str(plugins_dir(tmp_path)), **environ})


def _client(app, make_user, login, name, role="admin", **extra):
    client = app.test_client()
    login(client, make_user(name, role=role, **extra))
    return client


# ── Default and upgrades ─────────────────────────────────────────────────────


def test_new_installations_have_external_plugins_off(app_factory, tmp_path, make_user, login):
    app = _build(app_factory, tmp_path)
    assert app.extensions["bananawiki.plugins"].allowed is False
    owner = _client(app, make_user, login, "boss", role="owner")
    page = owner.get("/admin/plugins").get_data(as_text=True)
    assert 'name="plugin_file"' not in page and "BW_ALLOW_EXTERNAL_PLUGINS=1" in page
    upload(owner, archive(plugin_files()), password=PASSWORD)
    assert row(app, "uploaded") is None


def test_an_upgraded_installation_with_plugins_keeps_them(app_factory, tmp_path, caplog):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    set_row(first, "hello_plugin", True)
    with caplog.at_level(logging.WARNING, logger="bananawiki.plugins"):
        app = _build(app_factory, tmp_path)
    assert app.extensions["bananawiki.plugins"].allowed is True
    assert "hello_plugin" in app.extensions["bananawiki.registry"].features
    assert any("BW_ALLOW_EXTERNAL_PLUGINS" in record.getMessage() for record in caplog.records)


def test_an_empty_value_turns_plugins_off_as_before(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="")
    assert app.extensions["bananawiki.plugins"].allowed is False


def test_the_operator_choice_wins_over_detection(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    set_row(first, "hello_plugin", True)
    off = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="0")
    assert off.extensions["bananawiki.plugins"].allowed is False
    assert "hello_plugin" not in off.extensions["bananawiki.registry"].features
    empty = tmp_path / "other"
    on = app_factory(environ={"BW_EXTERNAL_PLUGINS_DIR": str(empty), "BW_ALLOW_EXTERNAL_PLUGINS": "1"})
    assert on.extensions["bananawiki.plugins"].allowed is True


def test_managed_hosting_still_needs_container_isolation(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = _build(app_factory, tmp_path, BW_MANAGED_HOSTING="1")
    assert app.extensions["bananawiki.plugins"].allowed is False


# ── Owner and superusers only ────────────────────────────────────────────────


def test_a_plain_administrator_cannot_upload_or_enable(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    admin = _client(app, make_user, login, "plain_admin")
    page = admin.get("/admin/plugins").get_data(as_text=True)
    assert 'name="plugin_file"' not in page
    assert "Only the owner or a superuser" in page
    upload(admin, archive(plugin_files()), password=PASSWORD)
    assert row(app, "uploaded") is None
    admin.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD})
    assert row(app, "hello_plugin")["enabled"] == 0
    assert not list((tmp_path / "instance").glob("plugin_safety_snapshots/*"))


def test_a_plain_administrator_cannot_enable_external_dependencies(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    admin = _client(app, make_user, login, "plain_admin")
    admin.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD, "force": "1"})
    assert row(app, "hello_plugin")["enabled"] == 0


def test_a_plain_administrator_may_still_switch_plugins_off(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    set_row(first, "hello_plugin", True)
    app = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    admin = _client(app, make_user, login, "plain_admin")
    admin.post("/admin/plugins/hello_plugin/disable")
    assert row(app, "hello_plugin")["enabled"] == 0


def test_the_owner_and_superusers_can_upload_and_enable(app_factory, tmp_path, make_user, login, db):
    app = _build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="1")
    owner = _client(app, make_user, login, "boss", role="owner")
    assert 'name="plugin_file"' in owner.get("/admin/plugins").get_data(as_text=True)
    assert upload(owner, archive(plugin_files()), password=PASSWORD).status_code == 302
    assert row(app, "uploaded")["enabled"] == 0
    superuser = make_user("trusted_su", role="admin")
    with app.app_context(), connection_scope() as session:
        session.execute("UPDATE users SET is_superuser = 1 WHERE id = ?", (superuser["id"],))
    client = app.test_client()
    login(client, superuser)
    client.post("/admin/plugins/uploaded/enable", data={"password": PASSWORD})
    assert row(app, "uploaded")["enabled"] == 1
