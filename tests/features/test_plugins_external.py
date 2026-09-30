"""Third-party plugins: loading at start-up, uploads, ZIP attacks, enabling, deleting and the 1.4 adapter."""

from __future__ import annotations

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope

from .plugins_support import (
    HELLO_PLUGIN,
    LEGACY_EXAMPLES,
    archive,
    build,
    install_folder,
    plugin_files,
    plugins_dir,
    row,
    set_row,
    start_enabled,
    upload,
    write_plugin,
)

PASSWORD = "correct horse battery"


def _tables(app) -> set[str]:
    with app.app_context(), connection_scope() as session:
        return set(session.column("SELECT name FROM sqlite_master WHERE type = 'table'"))


def _admin_client(app, make_user, login, name="root_admin"):
    client = app.test_client()
    login(client, make_user(name, role="admin"))
    return client


# ── Loading ──────────────────────────────────────────────────────────────────


def test_new_folder_is_registered_disabled_and_not_loaded(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = build(app_factory, tmp_path)
    assert row(app, "hello_plugin")["enabled"] == 0
    assert row(app, "hello_plugin")["builtin"] == 0
    assert "hello_plugin" not in app.extensions["bananawiki.registry"].features
    assert "hello_plugin__counts" not in _tables(app)


def test_example_plugin_loads_when_enabled(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin")
    assert app.extensions["bananawiki.plugins"].plugins["hello_plugin"].loaded
    assert "hello_plugin__counts" in _tables(app)
    client = app.test_client()
    assert client.get("/hello").status_code == 302  # private by default
    login(client, make_user("reader"))
    page = client.get("/hello")
    assert page.status_code == 200 and "Hello, reader!" in page.get_data(as_text=True)
    with app.test_request_context(), connection_scope() as session:
        registry.emit("page.created", page={"id": 1}, author_id=None)
        assert session.scalar("SELECT value FROM hello_plugin__counts WHERE name = 'pages_created'") == 1


def test_disabling_a_loaded_plugin_closes_it_at_once(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin")
    client = _admin_client(app, make_user, login)
    assert client.get("/hello").status_code == 200
    client.post("/admin/plugins/hello_plugin/disable")
    assert row(app, "hello_plugin")["enabled"] == 0
    assert client.get("/hello").status_code == 404
    assert "code unloads at restart" in client.get("/admin/plugins").get_data(as_text=True)


def test_external_plugins_switched_off_load_nothing(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = build(app_factory, tmp_path)
    set_row(first, "hello_plugin", True)
    app = build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="0")
    assert "hello_plugin" not in app.extensions["bananawiki.registry"].features


def test_managed_hosting_needs_container_isolation(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = build(app_factory, tmp_path)
    set_row(first, "hello_plugin", True)
    app = build(app_factory, tmp_path, BW_MANAGED_HOSTING="1")
    assert "hello_plugin" not in app.extensions["bananawiki.registry"].features
    isolated = build(app_factory, tmp_path, BW_MANAGED_HOSTING="1", BW_PLUGIN_ISOLATION="container")
    assert "hello_plugin" in isolated.extensions["bananawiki.registry"].features


def test_denylisted_plugin_is_not_loaded(app_factory, tmp_path):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin", BW_MANAGED_PLUGIN_DENYLIST="hello_plugin")
    assert "hello_plugin" not in app.extensions["bananawiki.registry"].features


@pytest.mark.parametrize("plugin_id", ["plugin_manager", "kanban", "banana_ai", "Auth"])
def test_folder_cannot_take_a_built_in_or_retired_id(app_factory, tmp_path, plugin_id):
    write_plugin(tmp_path, plugin_id, "raise SystemExit('must not run')\n")
    app = build(app_factory, tmp_path)
    state = app.extensions["bananawiki.plugins"].invalid[plugin_id]
    assert state.error_key == "plugins.error.id_reserved"
    assert not state.loaded and plugin_id not in app.extensions["bananawiki.plugins"].plugins
    if plugin_id in ("banana_ai", "Auth"):
        assert row(app, plugin_id) is None


def test_broken_plugin_does_not_stop_the_wiki(app_factory, tmp_path, make_user, login):
    write_plugin(tmp_path, "broken", "raise RuntimeError('boom')\n")
    app = start_enabled(app_factory, tmp_path, "broken")
    state = app.extensions["bananawiki.plugins"].plugins["broken"]
    assert not state.loaded and "boom" in state.error_values["detail"]
    client = _admin_client(app, make_user, login)
    assert "boom" in client.get("/admin/plugins").get_data(as_text=True)


def test_bad_manifest_and_misnamed_folders_are_skipped(app_factory, tmp_path):
    folder = plugins_dir(tmp_path) / "bad"
    folder.mkdir()
    (folder / "plugin.json").write_text("{not json")
    write_plugin(tmp_path, "other_name", "", manifest={"id": "different"})
    app = build(app_factory, tmp_path)
    invalid = app.extensions["bananawiki.plugins"].invalid
    assert invalid["bad"].error_key == "plugins.error.manifest_invalid_json"
    assert invalid["other_name"].error_key == "plugins.error.folder_name"
    assert row(app, "bad") is None and row(app, "different") is None


def test_schema_may_only_create_prefixed_tables(app_factory, tmp_path):
    init = "from bananawiki.wiki.registry import Feature\nFEATURE = Feature(id='sneaky', name='x')\n"
    write_plugin(tmp_path, "sneaky", init, schema="def upgrade(conn):\n    conn.execute('CREATE TABLE evil (x)')\n")
    app = start_enabled(app_factory, tmp_path, "sneaky")
    state = app.extensions["bananawiki.plugins"].plugins["sneaky"]
    assert not state.loaded and state.error_key == "plugins.error.load_failed"
    assert "evil" not in _tables(app)


def test_schema_cannot_write_core_tables(app_factory, tmp_path):
    init = "from bananawiki.wiki.registry import Feature\nFEATURE = Feature(id='sneaky', name='x')\n"
    schema = ("def upgrade(conn):\n    conn.execute('CREATE TABLE sneaky__ok (x)')\n"
              "    conn.execute(\"UPDATE users SET role = 'owner'\")\n")
    write_plugin(tmp_path, "sneaky", init, schema=schema)
    app = start_enabled(app_factory, tmp_path, "sneaky")
    assert not app.extensions["bananawiki.plugins"].plugins["sneaky"].loaded
    assert "sneaky__ok" not in _tables(app)  # the whole upgrade was rolled back


def test_plain_blueprints_of_plugins_are_gated(app_factory, tmp_path, make_user, login):
    init = (
        "from flask import Blueprint\nfrom bananawiki.wiki.registry import Feature\n"
        "bp = Blueprint('plain', __name__)\n"
        "@bp.get('/plain')\ndef view():\n    return 'plain page'\n"
        "FEATURE = Feature(id='plain', name='x', blueprints=[bp])\n"
    )
    write_plugin(tmp_path, "plain", init)
    app = start_enabled(app_factory, tmp_path, "plain")
    client = _admin_client(app, make_user, login)
    assert client.get("/plain").get_data(as_text=True) == "plain page"
    set_row(app, "plain", False)
    assert client.get("/plain").status_code == 404


# ── Uploads ──────────────────────────────────────────────────────────────────


def test_upload_needs_the_password_and_installs_disabled(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    content = archive(plugin_files())
    upload(client, content, password="wrong password")
    assert row(app, "uploaded") is None
    upload(client, content, password="")
    assert row(app, "uploaded") is None
    response = upload(client, content, password=PASSWORD)
    assert response.status_code == 302
    assert row(app, "uploaded")["enabled"] == 0
    assert (plugins_dir(tmp_path) / "uploaded" / "__init__.py").is_file()
    assert "uploaded" not in app.extensions["bananawiki.registry"].features
    text = client.get("/admin/plugins").get_data(as_text=True)
    assert "run with full privileges" in text
    detail = client.get("/admin/plugins/uploaded")
    assert detail.status_code == 200 and "Delete plugin" in detail.get_data(as_text=True)


def test_upload_in_one_top_level_folder(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    files = {f"uploaded/{name}": body for name, body in plugin_files().items()}
    upload(client, archive(files), password=PASSWORD)
    assert (plugins_dir(tmp_path) / "uploaded" / "plugin.json").is_file()


def test_upload_is_refused_when_external_plugins_are_off(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="0")
    client = _admin_client(app, make_user, login)
    assert 'name="plugin_file"' not in client.get("/admin/plugins").get_data(as_text=True)
    upload(client, archive(plugin_files()), password=PASSWORD)
    assert row(app, "uploaded") is None


def test_upload_needs_an_admin(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    client = app.test_client()
    login(client, make_user("editor9", role="editor"))
    assert upload(client, archive(plugin_files()), password=PASSWORD).status_code == 403
    assert row(app, "uploaded") is None


_BIG = b"0" * (11 * 1024 * 1024)


@pytest.mark.parametrize("files,links", [
    ({**plugin_files(), "../evil.py": "x"}, ()),
    ({**plugin_files(), "/etc/evil.py": "x"}, ()),
    ({**plugin_files(), "C:/evil.py": "x"}, ()),
    (plugin_files(), ("link.py",)),
    ({**plugin_files(), "native.so": "x"}, ()),
    ({**plugin_files(), "cached.pyc": "x"}, ()),
    ({**plugin_files(), "bomb.txt": b"0" * (5 * 1024 * 1024)}, ()),
    ({**plugin_files(), "big.bin": _BIG}, ()),
    ({**plugin_files(), "sub/plugin.json": "{}"}, ()),
    ({"plugin.json": plugin_files()["plugin.json"]}, ()),
    ({"a/plugin.json": plugin_files()["plugin.json"], "a/__init__.py": "", "b/other.py": ""}, ()),
    (plugin_files(plugin_id="kanban"), ()),
    (plugin_files(plugin_id="plugin_manager"), ()),
    (plugin_files(plugin_id="file_manager"), ()),
    (plugin_files(plugin_id="bad__id"), ()),
    (plugin_files(plugin_id="../up"), ()),
    (plugin_files(builtin=True), ()),
    (plugin_files(name=["not text"]), ()),
    (plugin_files(min_bananawiki="99.0"), ()),
    (plugin_files(bananawiki_api_version="2.0"), ()),
])
def test_dangerous_archives_are_refused(app_factory, tmp_path, make_user, login, files, links):
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    upload(client, archive(files, links=links), password=PASSWORD)
    with app.app_context(), connection_scope() as session:
        assert session.scalar("SELECT COUNT(*) FROM plugins WHERE builtin = 0") == 0
    assert [p.name for p in plugins_dir(tmp_path).iterdir()] == []
    assert not (tmp_path / "evil.py").exists()


def test_too_many_entries_are_refused(app_factory, tmp_path, make_user, login, monkeypatch):
    from bananawiki.wiki.features.plugin_manager import archive as archive_module

    monkeypatch.setattr(archive_module, "MAX_FILES", 5)
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    files = {**plugin_files(), **{f"f{i}.txt": "x" for i in range(5)}}
    upload(client, archive(files), password=PASSWORD)
    assert row(app, "uploaded") is None


def test_second_upload_with_the_same_id_is_refused(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    upload(client, archive(plugin_files()), password=PASSWORD)
    other = plugin_files(plugin_id="Uploaded")
    upload(client, archive(other), password=PASSWORD)
    assert [p.name for p in plugins_dir(tmp_path).iterdir()] == ["uploaded"]


def test_non_bwplugin_file_is_refused(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    upload(client, archive(plugin_files()), password=PASSWORD, filename="plugin.zip")
    assert row(app, "uploaded") is None


# ── Enabling and deleting ────────────────────────────────────────────────────


def test_enabling_asks_for_the_password_and_backs_up_first(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    page = client.post("/admin/plugins/hello_plugin/enable")
    assert page.status_code == 200 and 'name="password"' in page.get_data(as_text=True)
    assert row(app, "hello_plugin")["enabled"] == 0
    client.post("/admin/plugins/hello_plugin/enable", data={"password": "nope nope nope"})
    assert row(app, "hello_plugin")["enabled"] == 0
    client.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD})
    assert row(app, "hello_plugin")["enabled"] == 1
    snapshots = list((tmp_path / "instance" / "plugin_safety_snapshots").glob("*-hello_plugin.db"))
    assert len(snapshots) == 1
    text = client.get("/admin/plugins").get_data(as_text=True)
    assert "restart needed" in text
    assert "hello_plugin" not in app.extensions["bananawiki.registry"].features  # loads at the next start


def test_enabling_is_refused_when_the_backup_fails(app_factory, tmp_path, make_user, login, monkeypatch):
    from bananawiki.wiki.features.plugin_manager import service

    install_folder(tmp_path, HELLO_PLUGIN)
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)

    def fail(_plugin_id):
        raise OSError("disk full")

    monkeypatch.setattr(service, "backup_database", fail)
    client.post("/admin/plugins/hello_plugin/enable", data={"password": PASSWORD})
    assert row(app, "hello_plugin")["enabled"] == 0


def test_missing_requirement_is_offered(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    init = "from bananawiki.wiki.registry import Feature\nFEATURE = Feature(id='needs_hello', name='x')\n"
    write_plugin(tmp_path, "needs_hello", init, manifest={"requires": ["hello_plugin"]})
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    page = client.post("/admin/plugins/needs_hello/enable").get_data(as_text=True)
    assert "Enable them too" in page
    client.post("/admin/plugins/needs_hello/enable", data={"enable_deps": "1", "password": PASSWORD})
    assert row(app, "needs_hello")["enabled"] == 1 and row(app, "hello_plugin")["enabled"] == 1
    page = client.post("/admin/plugins/hello_plugin/disable").get_data(as_text=True)
    assert "Disable them too" in page
    client.post("/admin/plugins/hello_plugin/disable", data={"cascade": "1"})
    assert row(app, "needs_hello")["enabled"] == 0 and row(app, "hello_plugin")["enabled"] == 0


def test_delete_asks_for_the_password_and_drops_only_listed_tables(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin")
    with app.app_context(), connection_scope() as session:
        session.execute("CREATE TABLE hello_plugin_x__data (x)")  # another id's prefix
    client = _admin_client(app, make_user, login)
    page = client.post("/admin/plugins/hello_plugin/delete").get_data(as_text=True)
    assert "hello_plugin__counts" in page and "hello_plugin_x__data" not in page
    assert row(app, "hello_plugin") is not None
    client.post("/admin/plugins/hello_plugin/delete", data={
        "password": PASSWORD, "drop_data": "1", "data_table": ["hello_plugin__counts", "users"],
    })
    assert row(app, "hello_plugin") is None
    assert not (plugins_dir(tmp_path) / "hello_plugin").exists()
    tables = _tables(app)
    assert "hello_plugin__counts" not in tables and "users" in tables and "hello_plugin_x__data" in tables
    assert client.get("/hello").status_code == 404


def test_delete_keeps_data_unless_ticked(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    app = start_enabled(app_factory, tmp_path, "hello_plugin")
    client = _admin_client(app, make_user, login)
    client.post("/admin/plugins/hello_plugin/delete",
                data={"password": PASSWORD, "data_table": ["hello_plugin__counts"]})
    assert row(app, "hello_plugin") is None
    assert "hello_plugin__counts" in _tables(app)


def test_row_without_files_can_be_removed(app_factory, tmp_path, make_user, login):
    app = build(app_factory, tmp_path)
    with app.app_context(), connection_scope() as session:
        session.execute("INSERT INTO plugins (id, name, version, builtin, enabled) VALUES ('gone', 'Gone', '1', 0, 1)")
    client = _admin_client(app, make_user, login)
    assert "files are missing" in client.get("/admin/plugins").get_data(as_text=True)
    client.post("/admin/plugins/gone/delete")
    assert row(app, "gone") is None


def test_plugin_kit_download(app, admin_client):
    import io
    import zipfile

    response = admin_client.get("/admin/plugins/sdk-download")
    names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
    assert "bananawiki-plugin-kit/PLUGINS.md" in names
    assert "bananawiki-plugin-kit/hello_plugin/plugin.json" in names


# ── BananaWiki 1.4 plugins ───────────────────────────────────────────────────


def test_legacy_word_count_plugin_runs_through_the_adapter(app_factory, tmp_path, make_user):
    install_folder(tmp_path, LEGACY_EXAMPLES / "page_word_count")
    app = start_enabled(app_factory, tmp_path, "page_word_count")
    state = app.extensions["bananawiki.plugins"].plugins["page_word_count"]
    assert state.loaded and state.legacy
    user = make_user("writer")
    with app.test_request_context(), connection_scope():
        registry.emit("page.updated", page={"id": 7, "content": "one two three"}, previous={}, author_id=user["id"])
        html = registry.render_slot("page.below_content", page={"id": 7})
    assert "3 words" in str(html)


def test_legacy_hello_world_route_is_gated(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, LEGACY_EXAMPLES / "hello_world")
    app = start_enabled(app_factory, tmp_path, "hello_world")
    client = _admin_client(app, make_user, login)
    assert "Hello, root_admin!" in client.get("/hello").get_data(as_text=True)
    set_row(app, "hello_world", False)
    assert client.get("/hello").status_code == 404


def test_legacy_db_helpers_keep_to_plugin_tables(app_factory, tmp_path):
    from bananawiki.sdk import compat

    install_folder(tmp_path, LEGACY_EXAMPLES / "page_word_count")
    app = start_enabled(app_factory, tmp_path, "page_word_count")
    with app.test_request_context(), connection_scope():
        compat.db_execute("INSERT INTO page_word_count__counts (page_id, word_count) VALUES (?, ?)", [1, 2])
        assert compat.db_query("SELECT word_count FROM page_word_count__counts")[0][0] == 2
        for sql in ("UPDATE users SET role = 'owner'", "DELETE FROM main.users",
                    "INSERT OR REPLACE INTO site_settings (id) VALUES (1)", "PRAGMA foreign_keys = OFF",
                    "ATTACH DATABASE ':memory:' AS other", "CREATE TABLE loose (x)"):
            with pytest.raises(compat.PluginError):
                compat.db_execute(sql)
        with pytest.raises(compat.PluginError):
            compat.db_query("DELETE FROM page_word_count__counts")
        with pytest.raises(compat.PluginError):
            compat.db_query("SELECT 1; DROP TABLE users")


def test_legacy_permissions_follow_role_defaults(app, make_user):
    from bananawiki.sdk import compat

    plugin = compat.Plugin("perm_demo")
    plugin.register_permission("perm_demo.view", "View", "", default_editor=True)
    with app.test_request_context(), connection_scope():
        assert compat.has_permission(make_user("ed2", role="editor"), "perm_demo.view")
        assert not compat.has_permission(make_user("us2"), "perm_demo.view")
        assert compat.has_permission(make_user("ad2", role="admin"), "perm_demo.view")
        assert not compat.has_permission(make_user("us3"), "unknown.key")


def test_folder_named_like_a_feature_leaves_the_feature_listed(app_factory, tmp_path, make_user, login):
    write_plugin(tmp_path, "plugin_manager", "")
    app = build(app_factory, tmp_path)
    client = _admin_client(app, make_user, login)
    text = client.get("/admin/plugins").get_data(as_text=True)
    assert "Folders that could not be read" in text
    assert "belongs to a built-in feature" in text
    assert client.get("/admin/plugins/plugin_manager").status_code == 200
    assert "Always on" in client.get("/admin/plugins/plugin_manager").get_data(as_text=True)
