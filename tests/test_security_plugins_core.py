"""Security regression tests for the plugin system core.

Each test names the audit finding it pins down.  The reproductions come
from the plugin audit (install, runtime and builtins areas) and from the
API boundary audit (storage quota).
"""

import io
import json
import os
import sqlite3
import sys
import time
import zipfile

import pytest

import config
import db
import plugin_loader
from bananawiki_sdk._exceptions import PluginAPIVersionError, PluginConfigError, PluginError

ADMIN_PASSWORD = "admin123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ext(tmp_path, monkeypatch):
    """A private external plugin folder and instance directory, self-hosted."""
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(plugin_loader, "EXTERNAL_DIR", str(external))
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", True)
    monkeypatch.setattr(config, "MANAGED_HOSTING", False)
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path / "instance"))
    return external


def _archive(manifest, init_src=None):
    """Return the bytes of a .bwplugin archive."""
    manifest = {"name": manifest["id"], "version": "1.0.0", **manifest}
    if init_src is None:
        init_src = f"from bananawiki_sdk import Plugin\nplugin = Plugin({manifest['id']!r})\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.json", json.dumps(manifest))
        zf.writestr("__init__.py", init_src)
    return buf.getvalue()


def _archive_file(tmp_path, manifest, init_src=None):
    path = tmp_path / f"upload-{time.monotonic_ns()}.bwplugin"
    path.write_bytes(_archive(manifest, init_src))
    return str(path)


def _upload(client, payload, password=ADMIN_PASSWORD):
    data = {"plugin_file": (io.BytesIO(payload), "plugin.bwplugin")}
    if password is not None:
        data["password"] = password
    return client.post(
        "/admin/plugins/import", data=data,
        content_type="multipart/form-data", follow_redirects=True,
    )


def _tables():
    with db.get_db_context() as conn:
        return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _route_plugin_source(plugin_id):
    """A plugin that adds a route and an app-wide before_request handler."""
    return (
        "from bananawiki_sdk import Plugin\n"
        f"plugin = Plugin({plugin_id!r})\n"
        "STATE = {'before': 0}\n"
        "@plugin.on_load\n"
        "def setup(app):\n"
        f"    @app.route('/{plugin_id}-ping')\n"
        f"    def {plugin_id}_ping():\n"
        "        return 'alive'\n"
        "    @app.before_request\n"
        f"    def {plugin_id}_before():\n"
        "        STATE['before'] += 1\n"
    )


def _install_and_enable(client, plugin_id, init_src=None):
    resp = _upload(client, _archive({"id": plugin_id}, init_src))
    assert db.get_plugin(plugin_id) is not None, resp.get_data(as_text=True)[:500]
    client.post(f"/admin/plugins/{plugin_id}/enable", data={"password": ADMIN_PASSWORD})
    assert db.is_plugin_enabled(plugin_id)


# ---------------------------------------------------------------------------
# A#0 and A#9: password re-entry, audit record and honest warnings
# ---------------------------------------------------------------------------

def test_import_requires_the_admins_password(ext, logged_in_admin):
    payload = _archive({"id": "pw_import"})
    _upload(logged_in_admin, payload, password=None)
    assert db.get_plugin("pw_import") is None
    resp = _upload(logged_in_admin, payload, password="not-the-password")
    assert db.get_plugin("pw_import") is None
    assert "Incorrect password" in resp.get_data(as_text=True)
    assert not (ext / "pw_import").exists()
    _upload(logged_in_admin, payload)
    row = db.get_plugin("pw_import")
    assert row is not None and not row["enabled"] and not row["builtin"]


def test_import_form_warns_that_plugins_get_complete_control(ext, logged_in_admin):
    html = logged_in_admin.get("/admin/plugins").get_data(as_text=True)
    assert "runs as the wiki itself" in html
    assert "complete control of the wiki" in html
    assert "not from an admin who installs code" in html
    assert 'name="password"' in html


def test_self_hosted_switch_removes_the_upload_form(ext, logged_in_admin, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", False)
    html = logged_in_admin.get("/admin/plugins").get_data(as_text=True)
    assert 'name="plugin_file"' not in html
    assert "BW_ALLOW_EXTERNAL_PLUGINS=0" in html
    resp = _upload(logged_in_admin, _archive({"id": "switched_off"}))
    assert "BW_ALLOW_EXTERNAL_PLUGINS=0" in resp.get_data(as_text=True)
    assert db.get_plugin("switched_off") is None


def test_enabling_external_code_asks_for_the_password_first(ext, logged_in_admin, monkeypatch):
    import routes.plugins as plugin_routes
    logged = []
    monkeypatch.setattr(plugin_routes, "log_action",
                        lambda action, _request, **details: logged.append((action, details)))
    _upload(logged_in_admin, _archive({"id": "pw_enable"}))

    resp = logged_in_admin.post("/admin/plugins/pw_enable/enable")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "complete control of the wiki" in html
    assert 'name="password"' in html
    assert not db.is_plugin_enabled("pw_enable")

    logged_in_admin.post("/admin/plugins/pw_enable/enable", data={"password": "wrong"})
    assert not db.is_plugin_enabled("pw_enable")
    assert any(action == "plugin_password_rejected" for action, _ in logged)

    logged_in_admin.post("/admin/plugins/pw_enable/enable", data={"password": ADMIN_PASSWORD})
    assert db.is_plugin_enabled("pw_enable")
    trusted = [details for action, details in logged if action == "plugin_code_trusted"]
    assert trusted and trusted[0]["plugin_id"] == "pw_enable"
    assert trusted[0]["password_confirmed"] is True


def test_enabling_a_builtin_needs_no_password(ext, logged_in_admin):
    db.disable_plugin("drafts")
    logged_in_admin.post("/admin/plugins/drafts/enable")
    assert db.is_plugin_enabled("drafts")


def test_deleting_an_external_plugin_asks_for_the_password(ext, logged_in_admin):
    _upload(logged_in_admin, _archive({"id": "pw_delete"}))
    resp = logged_in_admin.post("/admin/plugins/pw_delete/delete")
    assert resp.status_code == 200
    assert 'name="password"' in resp.get_data(as_text=True)
    assert db.get_plugin("pw_delete") is not None
    logged_in_admin.post("/admin/plugins/pw_delete/delete", data={"password": "wrong"})
    assert db.get_plugin("pw_delete") is not None
    logged_in_admin.post("/admin/plugins/pw_delete/delete", data={"password": ADMIN_PASSWORD})
    assert db.get_plugin("pw_delete") is None
    assert not (ext / "pw_delete").exists()


def test_onboarding_cannot_switch_on_uploaded_code(ext, logged_in_admin, tmp_path):
    """Onboarding's feature list is meant for built-ins.  It used to enable,
    and run, any plugin id posted to it, with no password and no snapshot."""
    marker = tmp_path / "ran"
    _upload(logged_in_admin, _archive(
        {"id": "via_onboarding"},
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "from bananawiki_sdk import Plugin\nplugin = Plugin('via_onboarding')\n",
    ))
    assert db.get_plugin("via_onboarding") is not None
    db.disable_plugin("canvas")
    logged_in_admin.post("/onboarding", data={
        "mode": "advanced", "plugins": ["via_onboarding", "canvas"],
    })
    assert not db.is_plugin_enabled("via_onboarding")
    assert not marker.exists()
    assert db.is_plugin_enabled("canvas")


def test_loader_runs_code_only_for_enabled_registry_rows(ext, tmp_path):
    marker = tmp_path / "ran"
    plugin_loader.import_bwplugin(_archive_file(
        tmp_path, {"id": "row_disabled"},
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n",
    ))
    from app import app
    assert plugin_loader.trigger_plugin_enable("row_disabled", app) is False
    assert not marker.exists()
    # The batch toggle only switches built-in rows on, but still switches
    # any row off.
    db.set_plugins_enabled(["row_disabled", "drafts"], enabled=False)
    db.set_plugins_enabled(["row_disabled", "drafts"], enabled=True)
    assert not db.is_plugin_enabled("row_disabled")
    assert db.is_plugin_enabled("drafts")


def test_external_plugin_log_warning_follows_location_not_path_text(tmp_path, monkeypatch):
    """banana_ops keeps external plugins in data/plugins, which never
    contained the text 'plugins/external' the old check looked for."""
    plugin_dir = tmp_path / "data" / "plugins" / "located_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("")
    warnings = []

    class _Recorder:
        def warning(self, message, *args):
            warnings.append(message % args)

    monkeypatch.setattr(plugin_loader, "_get_plugin_logger", lambda: _Recorder())
    try:
        plugin_loader._load_plugin_module(str(plugin_dir), {"id": "located_plugin"})
    finally:
        sys.modules.pop("_bw_plugin_located_plugin", None)
    assert any("full application privileges" in message for message in warnings)


# ---------------------------------------------------------------------------
# A#1 and A#14 (with A#15 and A#17 loader parts): built-in ids and rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plugin_id", ["audit", "Kanban", "CHAT"])
def test_upload_cannot_take_a_builtin_id(ext, tmp_path, plugin_id):
    target = plugin_id.lower()
    before = dict(db.get_plugin(target))
    with pytest.raises(PluginConfigError, match="built-in"):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": plugin_id, "name": "Evil"}))
    assert dict(db.get_plugin(target)) == before
    assert not any(ext.iterdir())
    assert [b for _d, m, b in plugin_loader.discover_plugins() if m["id"].lower() == target] == [True]


def test_upload_cannot_replace_an_existing_row(ext, tmp_path):
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "first_copy", "name": "First"}))
    db.enable_plugin("first_copy")
    with pytest.raises(PluginConfigError, match="already installed"):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "First_Copy", "name": "Second"}))
    row = db.get_plugin("first_copy")
    assert row["name"] == "First" and row["enabled"] == 1
    assert db.get_plugin("First_Copy") is None


def test_hand_placed_copy_of_a_builtin_is_ignored_and_the_row_repaired(ext):
    from app import app
    shadow = ext / "kanban"
    shadow.mkdir()
    (shadow / "plugin.json").write_text(json.dumps({"id": "kanban", "name": "K", "version": "9"}))
    (shadow / "__init__.py").write_text("raise RuntimeError('shadow copy must never run')\n")
    # What an old upload left behind: the built-in's row relabelled external.
    with db.get_db_context() as conn:
        conn.execute("UPDATE plugins SET builtin=0, name='Evil', version='9' WHERE id='kanban'")
        conn.commit()
    found = [(d, b) for d, m, b in plugin_loader.discover_plugins() if m["id"] == "kanban"]
    assert len(found) == 1 and found[0][1] is True
    plugin_loader.load_enabled_plugins(app)
    row = db.get_plugin("kanban")
    assert row["builtin"] == 1 and row["name"] != "Evil"
    assert not plugin_loader._loaded_plugins["kanban"]["dir"].startswith(str(ext))


def test_code_under_external_dir_is_external_whatever_the_row_says(ext, tmp_path, monkeypatch):
    from app import app
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "self_promoter"}))
    with db.get_db_context() as conn:
        conn.execute("UPDATE plugins SET builtin=1, enabled=1 WHERE id='self_promoter'")
        conn.commit()
    assert not plugin_loader.is_builtin_plugin("self_promoter")
    assert [b for _d, m, b in plugin_loader.discover_plugins() if m["id"] == "self_promoter"] == [False]
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", False)
    assert "self_promoter" not in {m["id"] for _d, m, _b in plugin_loader.discover_plugins()}
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", True)
    plugin_loader.load_enabled_plugins(app)
    assert db.get_plugin("self_promoter")["builtin"] == 0
    plugin_loader.trigger_plugin_disable("self_promoter")


def test_discovery_skips_duplicate_and_misnamed_external_folders(ext):
    for folder, plugin_id in (("dup_one", "dup_one"), ("Dup_One", "Dup_One"), ("renamed", "other_id")):
        path = ext / folder
        path.mkdir()
        (path / "plugin.json").write_text(json.dumps({"id": plugin_id, "name": "x", "version": "1"}))
        (path / "__init__.py").write_text("")
    external = [m["id"] for _d, m, b in plugin_loader.discover_plugins() if not b]
    assert external.count("dup_one") + external.count("Dup_One") == 1
    assert "other_id" not in external


# ---------------------------------------------------------------------------
# A#2 and A#8: "drop data" only ever drops the plugin's own tables
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plugin_id", ["u", "p", "c", "user", "page", "site", "api", "group"])
def test_drop_data_never_matches_core_tables(ext, tmp_path, plugin_id):
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": plugin_id}))
    with db.get_db_context() as conn:
        conn.execute(f'CREATE TABLE "{plugin_id}__own" (id INTEGER)')
        conn.commit()
    assert plugin_loader.plugin_data_tables(plugin_id) == [f"{plugin_id}__own"]
    before = _tables()
    assert plugin_loader.delete_external_plugin(plugin_id, drop_data=True)
    assert before - _tables() == {f"{plugin_id}__own"}


def test_drop_data_leaves_other_plugins_and_builtins_alone(ext, tmp_path):
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "foo"}))
    db.register_plugin("foo__bar", name="Hand installed", version="1")
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE foo__mine (id INTEGER)")
        conn.execute("CREATE TABLE foo__bar__theirs (id INTEGER)")
        conn.execute("CREATE TABLE Foo__other_case (id INTEGER)")
        conn.commit()
    assert plugin_loader.plugin_data_tables("foo") == ["foo__mine"]
    # Built-ins own no droppable tables: theirs are core schema.
    assert plugin_loader.plugin_data_tables("canvas") == []
    before = _tables()
    assert plugin_loader.delete_external_plugin("chat", drop_data=True)
    assert _tables() == before


def test_drop_data_respects_an_id_ending_in_an_underscore(ext, tmp_path):
    """'foo___theirs' starts with 'foo__' but belongs to the plugin 'foo_'."""
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "foo"}))
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "foo_"}))
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE foo__mine (id INTEGER)")
        conn.execute("CREATE TABLE foo___theirs (id INTEGER)")
        conn.commit()
    assert plugin_loader.plugin_data_tables("foo") == ["foo__mine"]
    assert plugin_loader.plugin_data_tables("foo_") == ["foo___theirs"]
    assert plugin_loader.delete_external_plugin("foo", drop_data=True)
    assert "foo___theirs" in _tables() and "foo__mine" not in _tables()


@pytest.mark.parametrize("plugin_id", ["user_profile_fields", "User_Profile_Fields"])
def test_builtin_table_namespaces_cannot_be_taken_or_dropped(ext, tmp_path, plugin_id):
    """user_profiles keeps its data in user_profile_fields__* tables, outside
    the core schema.  A plugin uploaded under that id used to be offered
    those tables to drop."""
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS user_profile_fields__values (id INTEGER)")
        conn.commit()
    with pytest.raises(PluginConfigError, match="reserved by a built-in"):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": plugin_id}))
    assert plugin_loader.plugin_data_tables("user_profile_fields") == []


def test_every_builtin_table_outside_the_schema_is_reserved():
    """A built-in that creates '<namespace>__' tables under a namespace that
    is not its id must list the namespace in _BUILTIN_TABLE_NAMESPACES."""
    import re
    from bananawiki_sdk._database import _core_tables
    pattern = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_][A-Za-z0-9_]*)", re.I)
    reserved = {name.casefold() for name in plugin_loader.builtin_plugin_ids()}
    reserved |= plugin_loader._BUILTIN_TABLE_NAMESPACES
    unreserved = []
    for root, _dirs, files in os.walk(plugin_loader.BUILTIN_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                for table in pattern.findall(fh.read()):
                    if "__" not in table or table.lower() in _core_tables():
                        continue
                    if table.split("__", 1)[0].casefold() not in reserved:
                        unreserved.append(table)
    assert unreserved == []


def test_drop_data_is_off_unless_ticked_and_limited_to_listed_tables(ext, logged_in_admin):
    for plugin_id in ("keep_data", "drop_listed"):
        _upload(logged_in_admin, _archive({"id": plugin_id}))
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE keep_data__rows (id INTEGER)")
        conn.execute("CREATE TABLE drop_listed__a (id INTEGER)")
        conn.execute("CREATE TABLE drop_listed__b (id INTEGER)")
        conn.commit()

    page = logged_in_admin.post("/admin/plugins/drop_listed/delete").get_data(as_text=True)
    assert "drop_listed__a" in page and "drop_listed__b" in page
    assert '<input type="checkbox" name="drop_data" value="1">' in page

    logged_in_admin.post("/admin/plugins/keep_data/delete", data={"password": ADMIN_PASSWORD})
    assert "keep_data__rows" in _tables()

    logged_in_admin.post("/admin/plugins/drop_listed/delete", data={
        "password": ADMIN_PASSWORD, "drop_data": "1",
        "data_table": ["drop_listed__a", "users", "site_settings"],
    })
    tables = _tables()
    assert "drop_listed__a" not in tables
    assert {"drop_listed__b", "users", "site_settings"} <= tables


def test_dropping_data_takes_a_snapshot_first(ext, tmp_path, logged_in_admin, monkeypatch):
    import routes.plugins as plugin_routes
    _upload(logged_in_admin, _archive({"id": "snap_drop"}))
    with db.get_db_context() as conn:
        conn.execute("CREATE TABLE snap_drop__rows (id INTEGER)")
        conn.execute("INSERT INTO snap_drop__rows VALUES (7)")
        conn.commit()
    form = {"password": ADMIN_PASSWORD, "drop_data": "1", "data_table": ["snap_drop__rows"]}

    real_snapshot = plugin_routes._create_plugin_safety_snapshot

    def failing_snapshot(_plugin_id):
        raise OSError("disk full")

    monkeypatch.setattr(plugin_routes, "_create_plugin_safety_snapshot", failing_snapshot)
    logged_in_admin.post("/admin/plugins/snap_drop/delete", data=form)
    assert db.get_plugin("snap_drop") is not None
    assert "snap_drop__rows" in _tables()

    monkeypatch.setattr(plugin_routes, "_create_plugin_safety_snapshot", real_snapshot)
    logged_in_admin.post("/admin/plugins/snap_drop/delete", data=form)
    assert db.get_plugin("snap_drop") is None
    assert "snap_drop__rows" not in _tables()
    folder = tmp_path / "instance" / "plugin_safety_snapshots"
    [snapshot] = [name for name in os.listdir(folder) if name.endswith("-snap_drop.db")]
    conn = sqlite3.connect(folder / snapshot)
    try:
        assert conn.execute("SELECT id FROM snap_drop__rows").fetchall() == [(7,)]
    finally:
        conn.close()


def test_list_page_delete_button_no_longer_posts_drop_data(ext, logged_in_admin):
    _upload(logged_in_admin, _archive({"id": "button_check"}))
    html = logged_in_admin.get("/admin/plugins").get_data(as_text=True)
    assert 'name="drop_data" value="1"' not in html


# ---------------------------------------------------------------------------
# A#3: a malformed manifest never half-installs or stops the wiki
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("name", {"x": 1}), ("version", 3), ("author", ["a"]), ("description", "d" * 5000), ("name", " "),
])
def test_malformed_manifest_field_is_refused_before_extraction(ext, tmp_path, field, value):
    with pytest.raises(PluginConfigError):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "halfway", field: value}))
    assert not any(ext.iterdir())
    assert db.get_plugin("halfway") is None


def test_failed_registration_leaves_no_folder(ext, tmp_path, monkeypatch):
    def broken_register(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "register_plugin", broken_register)
    with pytest.raises(sqlite3.OperationalError):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "no_leftovers"}))
    assert not any(ext.iterdir())


def test_bad_hand_placed_manifest_does_not_stop_startup(ext):
    from app import app
    bad = ext / "broken_manifest"
    bad.mkdir()
    (bad / "plugin.json").write_text(json.dumps({"id": "broken_manifest", "name": {"x": 1}, "version": "1"}))
    (bad / "__init__.py").write_text("")
    plugin_loader.load_enabled_plugins(app)
    assert db.get_plugin("broken_manifest") is None


def test_uploaded_manifest_cannot_claim_to_be_builtin(ext, tmp_path):
    with pytest.raises(PluginConfigError, match="built-in"):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "claims_builtin", "builtin": True}))


# ---------------------------------------------------------------------------
# A#4, A#10, A#11: disable and delete stop routes, handlers and hooks in
# every worker; enable reaches every worker
# ---------------------------------------------------------------------------

def test_disable_and_delete_close_routes_and_request_handlers(ext, logged_in_admin):
    _install_and_enable(logged_in_admin, "liveroute", _route_plugin_source("liveroute"))
    module = sys.modules["_bw_plugin_liveroute"]
    assert logged_in_admin.get("/liveroute-ping").status_code == 200
    before = module.STATE["before"]
    logged_in_admin.get("/")
    assert module.STATE["before"] > before

    logged_in_admin.post("/admin/plugins/liveroute/disable")
    assert logged_in_admin.get("/liveroute-ping").status_code == 404
    before = module.STATE["before"]
    logged_in_admin.get("/")
    assert module.STATE["before"] == before

    logged_in_admin.post("/admin/plugins/liveroute/enable", data={"password": ADMIN_PASSWORD})
    assert logged_in_admin.get("/liveroute-ping").status_code == 200

    logged_in_admin.post("/admin/plugins/liveroute/delete", data={"password": ADMIN_PASSWORD})
    assert db.get_plugin("liveroute") is None
    assert logged_in_admin.get("/liveroute-ping").status_code == 404


def test_disabled_plugin_error_handlers_give_way_to_the_ones_they_replaced(ext, logged_in_admin):
    source = (
        "from bananawiki_sdk import Plugin\n"
        "plugin = Plugin('errpage')\n"
        "@plugin.on_load\n"
        "def setup(app):\n"
        "    @app.errorhandler(404)\n"
        "    def errpage_404(error):\n"
        "        return 'errpage-404', 404\n"
        "    @app.errorhandler(LookupError)\n"
        "    def errpage_lookup(error):\n"
        "        return 'errpage-lookup', 500\n"
    )
    _install_and_enable(logged_in_admin, "errpage", source)
    assert logged_in_admin.get("/no-such-page-here").get_data(as_text=True) == "errpage-404"

    logged_in_admin.post("/admin/plugins/errpage/disable")
    resp = logged_in_admin.get("/no-such-page-here")
    assert resp.status_code == 404
    assert "errpage-404" not in resp.get_data(as_text=True)
    # Where the plugin replaced nothing, the error goes on as if it had
    # never registered a handler.
    from app import app
    handler = app.error_handler_spec[None][None][LookupError]
    with app.test_request_context("/"):
        with pytest.raises(KeyError):
            handler(KeyError("no handler while disabled"))


def test_disable_seen_through_the_registry_stops_everything_here(ext, logged_in_admin):
    """Another worker disabled the plugin: only the database row changed."""
    from bananawiki_sdk import emit_hook
    source = _route_plugin_source("otherworker") + (
        "from bananawiki_sdk import hook\n"
        "CALLS = []\n"
        "@hook('otherworker_event')\n"
        "def otherworker_hook(**kwargs):\n"
        "    CALLS.append(1)\n"
    )
    _install_and_enable(logged_in_admin, "otherworker", source)
    module = sys.modules["_bw_plugin_otherworker"]
    emit_hook("otherworker_event")
    assert module.CALLS == [1]

    db.disable_plugin("otherworker")
    assert "otherworker" in plugin_loader.get_loaded_plugins()
    assert logged_in_admin.get("/otherworker-ping").status_code == 404
    emit_hook("otherworker_event")
    assert module.CALLS == [1]
    before = module.STATE["before"]
    logged_in_admin.get("/")
    assert module.STATE["before"] == before


def test_enable_seen_through_the_registry_loads_the_plugin_here(ext, logged_in_admin):
    """Another worker enabled the plugin: this one loads it on its next request."""
    _upload(logged_in_admin, _archive({"id": "lateloader"}, _route_plugin_source("lateloader")))
    assert "lateloader" not in plugin_loader.get_loaded_plugins()
    db.enable_plugin("lateloader")
    assert logged_in_admin.get("/lateloader-ping").status_code == 200
    assert "lateloader" in plugin_loader.get_loaded_plugins()


def test_delete_and_reinstall_through_another_worker_retires_the_old_code(ext, tmp_path, logged_in_admin):
    """Another worker deleted the plugin and installed a new copy under the
    same id: this worker must drop the old code rather than switch it back on."""
    from bananawiki_sdk import emit_hook

    def source(tag, prefix="from bananawiki_sdk import Plugin\nplugin = Plugin('respawn')\n"):
        return prefix + (
            "from bananawiki_sdk import hook\n"
            "CALLS = []\n"
            "@hook('respawn_event')\n"
            "def respawn_hook(**kwargs):\n"
            f"    CALLS.append({tag!r})\n"
        )

    # The old copy adds a route and a before_request handler; the new one
    # only a hook, since a route cannot be registered twice before a restart.
    _install_and_enable(logged_in_admin, "respawn", source("old", _route_plugin_source("respawn")))
    old_module = sys.modules["_bw_plugin_respawn"]
    assert logged_in_admin.get("/respawn-ping").status_code == 200

    # What the other worker did: row and folder gone, then a new upload with
    # the same id, enabled.  Nothing ran in this process.
    db.remove_plugin("respawn")
    import shutil
    shutil.rmtree(ext / "respawn")
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "respawn"}, source("new")))
    with db.get_db_context() as conn:
        conn.execute("UPDATE plugins SET installed_at='2000-01-01 00:00:00' WHERE id='respawn'")
        conn.commit()
    db.enable_plugin("respawn")

    logged_in_admin.get("/")
    assert "respawn" in plugin_loader.get_loaded_plugins()
    assert sys.modules["_bw_plugin_respawn"] is not old_module
    emit_hook("respawn_event")
    assert old_module.CALLS == []
    assert sys.modules["_bw_plugin_respawn"].CALLS == ["new"]
    before = old_module.STATE["before"]
    assert logged_in_admin.get("/respawn-ping").status_code == 404
    logged_in_admin.get("/")
    assert old_module.STATE["before"] == before


def test_reinstall_through_another_worker_replaces_parked_code(ext, tmp_path, logged_in_admin):
    """This worker disabled the plugin, so its hooks are only parked here.
    Another worker then deleted it and installed a new copy under the same
    id, straight away.  Enabling it must run the new code in this worker,
    never the parked copy of the deleted one."""
    from bananawiki_sdk import emit_hook

    def source(tag, prefix="from bananawiki_sdk import Plugin\nplugin = Plugin('parked')\n"):
        return prefix + (
            "from bananawiki_sdk import hook\n"
            "CALLS = []\n"
            "@hook('parked_event')\n"
            "def parked_hook(**kwargs):\n"
            f"    CALLS.append({tag!r})\n"
        )

    _install_and_enable(logged_in_admin, "parked", source("old", _route_plugin_source("parked")))
    old_module = sys.modules["_bw_plugin_parked"]
    logged_in_admin.post("/admin/plugins/parked/disable")
    assert "parked" not in plugin_loader.get_loaded_plugins()

    # What the other worker did, with no pause in between: installed_at
    # must tell the two installations apart even within one second.
    db.remove_plugin("parked")
    import shutil
    shutil.rmtree(ext / "parked")
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "parked"}, source("new")))
    db.enable_plugin("parked")

    logged_in_admin.get("/")
    assert sys.modules["_bw_plugin_parked"] is not old_module
    emit_hook("parked_event")
    assert old_module.CALLS == []
    assert sys.modules["_bw_plugin_parked"].CALLS == ["new"]
    assert logged_in_admin.get("/parked-ping").status_code == 404


def test_hooks_registered_in_on_load_survive_disable_and_enable(ext, logged_in_admin):
    from bananawiki_sdk import emit_hook
    source = (
        "from bananawiki_sdk import Plugin, hook\n"
        "plugin = Plugin('onload_hooks')\n"
        "CALLS = []\n"
        "@plugin.on_load\n"
        "def setup(app):\n"
        "    @hook('onload_hooks_event')\n"
        "    def inner(**kwargs):\n"
        "        CALLS.append(1)\n"
    )
    _install_and_enable(logged_in_admin, "onload_hooks", source)
    module = sys.modules["_bw_plugin_onload_hooks"]
    emit_hook("onload_hooks_event")
    logged_in_admin.post("/admin/plugins/onload_hooks/disable")
    emit_hook("onload_hooks_event")
    logged_in_admin.post("/admin/plugins/onload_hooks/enable", data={"password": ADMIN_PASSWORD})
    emit_hook("onload_hooks_event")
    assert module.CALLS == [1, 1]


# ---------------------------------------------------------------------------
# A#12: hook and slot ownership comes from the manifest
# ---------------------------------------------------------------------------

def test_forged_hook_and_slot_ownership_is_overwritten(ext, logged_in_admin):
    from bananawiki_sdk._hooks import _hook_registry
    from bananawiki_sdk._slots import _slot_registry, render_slot
    source = (
        "from bananawiki_sdk import Plugin, template_slot\n"
        "fake = Plugin('audit')\n"
        "plugin = Plugin('audit')\n"
        "@fake.hook('after_login')\n"
        "def mislabel_spy(**kwargs):\n"
        "    pass\n"
        "def mislabel_banner(context):\n"
        "    return '<p id=mislabel-slot>still here</p>'\n"
        "mislabel_banner._bw_plugin_id = 'audit'\n"
        "template_slot('page.below_content')(mislabel_banner)\n"
    )
    _install_and_enable(logged_in_admin, "mislabel", source)
    module = sys.modules["_bw_plugin_mislabel"]
    assert module.mislabel_spy._bw_plugin_id == "mislabel"
    assert module.plugin.plugin_id == "mislabel"
    logged_in_admin.post("/admin/plugins/mislabel/disable")
    assert all(fn is not module.mislabel_spy for fn in _hook_registry.get("after_login", []))
    assert all(fn is not module.mislabel_banner for fn in _slot_registry.get("page.below_content", []))
    from app import app
    with app.test_request_context("/"):
        from flask import g
        g.enabled_plugins = {"audit": True, "mislabel": False}
        assert "mislabel-slot" not in render_slot("page.below_content", {})


def test_slot_rendered_outside_a_request_follows_the_registry(ext, tmp_path):
    from bananawiki_sdk._slots import _slot_registry, render_slot, template_slot

    @template_slot("registry.gate.slot")
    def owned(context):
        return "owned-output"

    owned._bw_plugin_id = "registry_gate"
    try:
        db.register_plugin("registry_gate", name="Gate", version="1", enabled=False)
        assert "owned-output" not in render_slot("registry.gate.slot")
        db.enable_plugin("registry_gate")
        assert "owned-output" in render_slot("registry.gate.slot")
    finally:
        _slot_registry.pop("registry.gate.slot", None)


# ---------------------------------------------------------------------------
# A#13: the SDK database guard sees statements as SQLite parses them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE main.users SET role='admin' WHERE username='victim'",
    "UPDATE/**/users SET role='editor' WHERE username='victim'",
    "UPDATE OR REPLACE users SET role='owner' WHERE username='victim'",
    "INSERT INTO main.\"site_settings\" (id) VALUES (99)",
    "DELETE FROM user_sessions",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA foreign_keys=OFF",
    "CREATE TRIGGER sneaky AFTER INSERT ON pages BEGIN DELETE FROM users; END",
])
def test_db_execute_refuses_core_writes_however_spelled(sql):
    from bananawiki_sdk import db_execute
    db.create_user("victim", "x", role="user")
    with pytest.raises(PluginError):
        db_execute(sql)
    assert db.get_user_by_username("victim")["role"] == "user"


def test_db_query_is_read_only_even_through_a_cte():
    from bananawiki_sdk import db_query
    db.create_user("victim", "x", role="user")
    with pytest.raises(PluginError, match="read-only"):
        db_query("WITH x AS (SELECT 1) DELETE FROM users WHERE username='victim'")
    assert db.get_user_by_username("victim") is not None
    assert db_query("PRAGMA table_info(users)")


def test_db_execute_still_manages_plugin_tables():
    from bananawiki_sdk import db_execute, db_query
    db_execute("CREATE TABLE IF NOT EXISTS guard_ok__rows (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    db_execute("CREATE INDEX IF NOT EXISTS guard_ok__rows_v ON guard_ok__rows(v)")
    assert db_execute("INSERT INTO guard_ok__rows (v) VALUES (?)", ["a"]) == 1
    db_execute("INSERT OR REPLACE INTO guard_ok__rows (id, v) VALUES (1, 'b')")
    assert db_query("SELECT v FROM guard_ok__rows")[0]["v"] == "b"
    db_execute("DROP TABLE guard_ok__rows")


def test_every_core_schema_table_is_protected(tmp_path, monkeypatch):
    from bananawiki_sdk._database import _core_tables
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "schema-only.db"))
    db.init_db()
    with db.get_db_context() as conn:
        names = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            )
        }
    assert names, "init_db created no tables"
    assert sorted(name for name in names if name.lower() not in _core_tables()) == []


# ---------------------------------------------------------------------------
# A#5: a database snapshot is taken before external code first runs
# ---------------------------------------------------------------------------

def test_self_hosted_enable_takes_a_snapshot_first(ext, tmp_path, logged_in_admin):
    _install_and_enable(logged_in_admin, "snapshotted")
    folder = tmp_path / "instance" / "plugin_safety_snapshots"
    snapshots = [name for name in os.listdir(folder) if name.endswith("-snapshotted.db")]
    assert len(snapshots) == 1
    conn = sqlite3.connect(folder / snapshots[0])
    try:
        row = conn.execute("SELECT enabled FROM plugins WHERE id='snapshotted'").fetchone()
    finally:
        conn.close()
    assert row == (0,), "the snapshot must predate the enable"


def test_enable_is_refused_when_the_snapshot_fails(ext, logged_in_admin, monkeypatch):
    import routes.plugins as plugin_routes

    def failing_snapshot(_plugin_id):
        raise OSError("disk full")

    monkeypatch.setattr(plugin_routes, "_create_plugin_safety_snapshot", failing_snapshot)
    _upload(logged_in_admin, _archive({"id": "no_way_back"}))
    logged_in_admin.post("/admin/plugins/no_way_back/enable", data={"password": ADMIN_PASSWORD})
    assert not db.is_plugin_enabled("no_way_back")
    assert "no_way_back" not in plugin_loader.get_loaded_plugins()


def test_snapshot_pruning_keeps_platform_files(ext, tmp_path):
    import routes.plugins as plugin_routes
    folder = tmp_path / "instance" / "plugin_safety_snapshots"
    folder.mkdir(parents=True)
    keep = folder / "20200101T000000Z-operator-quarantine.db"
    keep.write_bytes(b"x")
    for index in range(7):
        old = folder / f"2020010{index}T000000Z-old{index}.db"
        old.write_bytes(b"x")
        os.utime(old, (1_000_000 + index, 1_000_000 + index))
    plugin_routes._create_plugin_safety_snapshot("fresh")
    ours = [name for name in os.listdir(folder) if "operator" not in name]
    assert len(ours) == 5
    assert keep.exists()


# ---------------------------------------------------------------------------
# A#6: the API version is checked at upload
# ---------------------------------------------------------------------------

def test_api_version_is_checked_at_upload(ext, tmp_path):
    with pytest.raises(PluginAPIVersionError):
        plugin_loader.import_bwplugin(
            _archive_file(tmp_path, {"id": "from_the_future", "bananawiki_api_version": "99.0"})
        )
    assert db.get_plugin("from_the_future") is None
    assert not any(ext.iterdir())


# ---------------------------------------------------------------------------
# A#7: staging folders are never discovered and are cleaned up
# ---------------------------------------------------------------------------

def test_staging_folders_are_skipped_and_stale_ones_removed(ext):
    from app import app
    stale = ext / ".ghost-abc12345"
    fresh = ext / ".staging-ghost2-abcd1234"
    for folder, plugin_id in ((stale, "ghost"), (fresh, "ghost2")):
        folder.mkdir()
        (folder / "plugin.json").write_text(json.dumps({"id": plugin_id, "name": "G", "version": "1"}))
        (folder / "__init__.py").write_text("")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    assert not any(not b for _d, _m, b in plugin_loader.discover_plugins())
    plugin_loader.load_enabled_plugins(app)
    assert db.get_plugin("ghost") is None and db.get_plugin("ghost2") is None
    assert not stale.exists()
    assert fresh.exists()


def test_import_stages_outside_discoverable_names(ext, tmp_path, monkeypatch):
    seen = []
    real_register = db.register_plugin

    def spy_register(*args, **kwargs):
        seen.extend(os.listdir(ext))
        return real_register(*args, **kwargs)

    monkeypatch.setattr(db, "register_plugin", spy_register)
    plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "staged"}))
    assert seen and all(name.startswith(".staging-staged-") for name in seen)
    assert os.listdir(ext) == ["staged"]


# ---------------------------------------------------------------------------
# A#19 loader part: the managed-hosting denylist applies at upload and discovery
# ---------------------------------------------------------------------------

def test_denylist_applies_at_upload_and_discovery(ext, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    with pytest.raises(PluginConfigError, match="not allowed"):
        plugin_loader.import_bwplugin(_archive_file(tmp_path, {"id": "File_Manager"}))
    planted = ext / "banana_ai"
    planted.mkdir()
    (planted / "plugin.json").write_text(json.dumps({"id": "banana_ai", "name": "B", "version": "1"}))
    (planted / "__init__.py").write_text("")
    assert "banana_ai" not in {m["id"] for _d, m, _b in plugin_loader.discover_plugins()}


# ---------------------------------------------------------------------------
# A#31 and A#44: the built-in path gate
# ---------------------------------------------------------------------------

def test_canvas_api_is_gated_with_the_canvas_plugin(client, admin_user):
    db.disable_plugin("canvas")
    assert client.get("/api/canvas/list-order-version").status_code == 404
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert client.get("/api/canvas/list-order-version").status_code == 404
    db.enable_plugin("canvas")
    assert client.get("/api/canvas/list-order-version").status_code == 200


@pytest.mark.parametrize("slug", [
    "history-of-rome", "tts-guide", "assessment-rules", "tag", "protection",
    "reserve", "propose-edit", "attachments", "contribution", "revert",
])
def test_disabled_plugins_do_not_hide_pages_named_like_their_routes(client, admin_user, slug):
    db.create_page(slug, slug, "body " + slug, user_id=admin_user)
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    for plugin_id in ("page_history", "tts", "assessments", "difficulty_tags",
                      "page_governance", "attachments"):
        db.disable_plugin(plugin_id)
    assert client.get(f"/page/{slug}").status_code == 200


def test_disabled_plugin_routes_under_a_page_stay_closed(client, admin_user):
    db.create_page("gated", "gated", "body", user_id=admin_user)
    client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    for plugin_id in ("page_history", "tts", "difficulty_tags"):
        db.disable_plugin(plugin_id)
    assert client.get("/page/gated/history").status_code == 404
    assert client.get("/page/gated/tts/status").status_code == 404
    assert client.post("/page/gated/tag", data={"difficulty_tag": "beginner"}).status_code == 404


# ---------------------------------------------------------------------------
# B#6: managed hosting does not accept bodies of unknown size
# ---------------------------------------------------------------------------

def test_body_size_unknown_only_for_undeclared_bodies():
    from helpers._storage_quota import body_size_unknown
    assert body_size_unknown(None, {"HTTP_TRANSFER_ENCODING": "chunked"})
    assert not body_size_unknown(10, {"HTTP_TRANSFER_ENCODING": "chunked"})
    assert not body_size_unknown(None, {})
    assert not body_size_unknown(0, {})


def test_managed_hosting_refuses_chunked_bodies(client, admin_user, monkeypatch, tmp_path):
    from helpers import _storage_quota
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STORAGE_LIMIT_BYTES", 50 * 1024 * 1024)
    _storage_quota.invalidate_storage_usage_cache()
    body = json.dumps({"title": "Big", "content": "b" * 4096}).encode()
    chunked = client.open(
        "/api/v1/pages", method="POST",
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        input_stream=io.BytesIO(body), environ_overrides={"wsgi.input_terminated": True},
    )
    assert chunked.status_code == 411
    declared = client.post("/api/v1/pages", headers={"Content-Type": "application/json"}, data=body)
    assert declared.status_code != 411


@pytest.mark.parametrize("method", ["DELETE", "GET"])
def test_managed_hosting_refuses_chunked_bodies_for_every_method(client, admin_user, monkeypatch, method):
    monkeypatch.setattr(config, "MANAGED_HOSTING", True)
    resp = client.open(
        "/api/v1/pages/anything", method=method,
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        input_stream=io.BytesIO(b"{}"), environ_overrides={"wsgi.input_terminated": True},
    )
    assert resp.status_code == 411


def test_self_hosted_wikis_still_accept_chunked_bodies(client, admin_user, monkeypatch):
    monkeypatch.setattr(config, "MANAGED_HOSTING", False)
    resp = client.open(
        "/login", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Transfer-Encoding": "chunked"},
        input_stream=io.BytesIO(b"username=admin&password=admin123"),
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert resp.status_code != 411
