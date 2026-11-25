"""Schema version 23: the per-feature access settings become permissions and each item's visibility.

Each test builds a wiki at version 22 with the old settings, upgrades it, and
checks that the people who could see, change, create or manage something
still can (and nobody else), except for the new defaults of the base roles.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations
from bananawiki.wiki.migrations import v23_permissions

USERS = {"own": "owner", "adm": "admin", "ed1": "editor", "ed2": "editor", "us1": "user", "us2": "user"}


def _database(path: Path, upto: int | None = None) -> Database:
    chain = migrations.MIGRATIONS if upto is None else migrations.MIGRATIONS[: upto - migrations.BASELINE]
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=chain,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _version_22(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    for user_id, role in USERS.items():
        conn.execute("INSERT INTO users (id, username, password, role) VALUES (?, ?, 'x', ?)", (user_id, user_id, role))
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    assert _database(path, upto=22).initialize() == 19
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _upgrade(path: Path, conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.commit()
    conn.close()
    assert _database(path, upto=23).initialize() == 1
    upgraded = sqlite3.connect(path)
    upgraded.row_factory = sqlite3.Row
    return upgraded


def _settings(conn, **values):
    for column, value in values.items():
        conn.execute(f"UPDATE site_settings SET {column} = ?", (value,))


def _board(conn, title, visibility="public", creator="adm"):
    return conn.execute("INSERT INTO kanban_boards (title, created_by, visibility) VALUES (?, ?, ?)",
                        (title, creator, visibility)).lastrowid


def _canvas(conn, slug, visibility="public", creator="adm", archived=0):
    return conn.execute("INSERT INTO canvas__layouts (slug, title, creator_id, visibility, is_archived) "
                        "VALUES (?, ?, ?, ?, ?)", (slug, slug, creator, visibility, archived)).lastrowid


def _role_changes(conn):
    return {(row["role"], row["permission_key"]): row["granted"] for row in conn.execute(
        "SELECT * FROM base_role_permissions")}


def _visibility(conn, table, item_id):
    return conn.execute(f"SELECT visibility FROM {table} WHERE id = ?", (item_id,)).fetchone()[0]


def _board_shares(conn, board_id):
    return sorted((row["share_type"], row["target"], row["access_level"]) for row in conn.execute(
        "SELECT * FROM kanban_board_shares WHERE board_id = ?", (board_id,)))


def _canvas_shares(conn, layout_id):
    return sorted((row["user_id"] or "", row["role"] or "", row["permission"]) for row in conn.execute(
        "SELECT * FROM canvas__permissions WHERE layout_id = ?", (layout_id,)))


def _keys(conn, kind, owner):
    table, column = ("custom_role_permissions", "role_id") if kind == "role" else ("user_permissions", "user_id")
    return {row[0] for row in conn.execute(f"SELECT permission_key FROM {table} WHERE {column} = ?", (owner,))}


def test_a_new_wiki_starts_from_the_built_in_defaults(tmp_path):
    database = _database(tmp_path / "fresh.db")
    database.initialize()
    conn = sqlite3.connect(tmp_path / "fresh.db")
    assert conn.execute("SELECT COUNT(*) FROM base_role_permissions").fetchone()[0] == 0
    assert "created_by" in {row[1] for row in conn.execute("PRAGMA table_info(pages)")}
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('a', 'a', 'x', 'admin')")
    board = conn.execute("INSERT INTO kanban_boards (title, created_by) VALUES ('B', 'a')").lastrowid
    assert _visibility(conn, "kanban_boards", board) == "private"  # the new default
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE kanban_boards SET visibility = 'shared'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO kanban_board_shares (board_id, share_type, target, access_level) "
                     "VALUES (?, 'user', 'a', 'write')", (board,))


# Settings at their defaults (only administrators reached boards and canvases)


def test_default_settings_keep_boards_and_canvases_to_their_people(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    public, shared, private = _board(conn, "P"), _board(conn, "S", "shared"), _board(conn, "X", "private")
    conn.execute("INSERT INTO kanban_board_shares (board_id, share_type, target, access_level) "
                 "VALUES (?, 'user', 'us1', 'write'), (?, 'role', 'editor', 'view')", (public, public))
    layout = _canvas(conn, "c")
    conn = _upgrade(tmp_path / "w.db", conn)
    # Nobody but administrators saw public items: they become private, keeping their personal shares.
    assert [_visibility(conn, "kanban_boards", b) for b in (public, shared, private)] == ["private"] * 3
    assert _visibility(conn, "canvas__layouts", layout) == "private"
    assert _board_shares(conn, public) == [("user", "us1", "edit")]  # the role share had no effect
    # Editors did not change every board or canvas: the new editor default is switched off on this wiki.
    assert _role_changes(conn) == {("editor", "kanban.edit_all"): 0, ("editor", "canvas.edit_all"): 0}


@pytest.mark.parametrize(("view", "write", "changes", "visibility", "role_shares"), [
    ("all", "all", {("user", "kanban.edit_all"): 1}, "members", []),
    ("all", "admin", {("editor", "kanban.edit_all"): 0}, "members", []),
    ("editor", "editor", {}, "private", [("role", "editor", "view")]),
    ("editor", "all", {("user", "kanban.edit_all"): 1}, "private", [("role", "editor", "view")]),
    ("admin", "editor", {}, "private", []),
])
def test_kanban_access_settings(tmp_path, view, write, changes, visibility, role_shares):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, kanban_access=view, kanban_write_access=write)
    board = _board(conn, "P")
    conn.execute("INSERT INTO kanban_board_shares (board_id, share_type, target, access_level) "
                 "VALUES (?, 'role', 'user', 'write')", (board,))
    conn = _upgrade(tmp_path / "w.db", conn)
    assert {k: v for k, v in _role_changes(conn).items() if k[1].startswith("kanban")} == changes
    assert _visibility(conn, "kanban_boards", board) == visibility
    kept_user_share = [("role", "user", "edit")] if view == "all" else []
    assert _board_shares(conn, board) == sorted(kept_user_share + role_shares)


def test_kanban_open_access_is_everyone_viewing_and_editing(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, kanban_open_access=1)
    public, private = _board(conn, "P"), _board(conn, "X", "private")
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _role_changes(conn) == {("user", "kanban.edit_all"): 1, ("editor", "canvas.edit_all"): 0}
    assert _visibility(conn, "kanban_boards", public) == "members"
    assert _visibility(conn, "kanban_boards", private) == "private"  # open access never showed private boards


@pytest.mark.parametrize(("view", "public_mode", "expected"), [
    ("all", 0, "public"),  # visitors read it as soon as public mode is on, as before
    ("all", 1, "public"),
    ("admin", 1, "public"),  # anyone could already read it by signing out
    ("admin", 0, "private"),  # members could not read it: nothing becomes visible by surprise
    ("editor", 0, "private"),
])
def test_anonymous_switch_on(tmp_path, view, public_mode, expected):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, kanban_access=view, kanban_public_access_enabled=1, public_mode=public_mode,
              canvas_access=view, canvas_public_access_enabled=1)
    board, layout = _board(conn, "P"), _canvas(conn, "c")
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _visibility(conn, "kanban_boards", board) == expected
    assert _visibility(conn, "canvas__layouts", layout) == expected


def test_an_expired_public_mode_counts_as_off(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, kanban_public_access_enabled=1, public_mode=1, public_mode_until="2001-01-01 00:00:00")
    board = _board(conn, "P")
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _visibility(conn, "kanban_boards", board) == "private"


# Canvases


def test_canvas_open_access_lets_every_member_open_and_edit(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, canvas_open_access=1)
    private, archived, public = _canvas(conn, "p", "private"), _canvas(conn, "a", "shared", archived=1), \
        _canvas(conn, "x")
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _role_changes(conn)[("user", "canvas.edit_all")] == 1
    assert ("editor", "canvas.edit_all") not in _role_changes(conn)
    assert [_visibility(conn, "canvas__layouts", c) for c in (private, archived, public)] == \
        ["members", "private", "members"]


def test_canvas_role_shares_and_levels(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, canvas_access="editor")
    layout = _canvas(conn, "c", "private")
    conn.execute("INSERT INTO canvas__permissions (layout_id, role, permission) VALUES (?, 'user', 'edit'), "
                 "(?, 'editor', 'edit'), (?, 'admin', 'view')", (layout, layout, layout))
    conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us1', 'view')",
                 (layout,))
    conn = _upgrade(tmp_path / "w.db", conn)
    # Roles without global access had no use of a role share; administrators see every canvas.
    assert _canvas_shares(conn, layout) == [("", "editor", "edit"), ("us1", "", "view")]


def test_a_refusal_that_mattered_becomes_explicit_shares(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, canvas_access="all")
    layout = _canvas(conn, "c")  # public: every member saw it ...
    conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us2', 'none')",
                 (layout,))  # ... except us2
    conn.execute("INSERT INTO canvas__permissions (layout_id, role, permission) VALUES (?, 'editor', 'edit')",
                 (layout,))
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _visibility(conn, "canvas__layouts", layout) == "private"
    assert _canvas_shares(conn, layout) == [("ed1", "", "edit"), ("ed2", "", "edit"), ("us1", "", "view")]


def test_a_role_refusal_too(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, canvas_access="all")
    layout = _canvas(conn, "c")
    conn.execute("INSERT INTO canvas__permissions (layout_id, role, permission) VALUES (?, 'user', 'none')",
                 (layout,))
    conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us1', 'view')",
                 (layout,))
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _visibility(conn, "canvas__layouts", layout) == "private"
    assert _canvas_shares(conn, layout) == [("ed1", "", "view"), ("ed2", "", "view"), ("us1", "", "view")]


def test_a_refusal_that_changed_nothing_is_just_dropped(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    layout = _canvas(conn, "c", "private")
    conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us2', 'none'), "
                 "(?, 'us1', 'view')", (layout, layout))
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _visibility(conn, "canvas__layouts", layout) == "private"
    assert _canvas_shares(conn, layout) == [("us1", "", "view")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us2', 'none')",
                     (layout,))


def test_a_refused_account_without_canvas_view_is_not_shared_with(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, canvas_access="all")
    layout = _canvas(conn, "c")
    conn.execute("INSERT INTO canvas__permissions (layout_id, user_id, permission) VALUES (?, 'us2', 'none')",
                 (layout,))
    conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES ('us1', 'read', 0)")
    conn.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES ('us1', 'page.view_all')")
    conn = _upgrade(tmp_path / "w.db", conn)
    assert _canvas_shares(conn, layout) == [("ed1", "", "view"), ("ed2", "", "view")]


def test_the_canvas_page_slug_triggers_survive_the_rebuild(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    conn = _upgrade(tmp_path / "w.db", conn)
    conn.execute("INSERT INTO canvas__layouts (slug, title, creator_id, data) VALUES ('c', 'c', 'adm', ?)",
                 ('{"nodes": [{"id": "n", "type": "wiki_page", "page_slug": "home"}], "edges": []}',))
    assert [tuple(row) for row in conn.execute("SELECT slug FROM canvas__page_slugs")] == [("home",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# Permission sets


@pytest.mark.parametrize("builder", ["admin", "editor", "user"])
def test_page_builder_access(tmp_path, builder):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, page_builder_access=builder)
    conn.execute("INSERT INTO custom_roles (id, name, base_role) VALUES (1, 'R', 'user')")
    conn.execute("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (1, 'page.view_all')")
    conn = _upgrade(tmp_path / "w.db", conn)
    reached = {"admin": set(), "editor": {"editor"}, "user": {"user", "editor"}}[builder]
    assert {role for (role, key), granted in _role_changes(conn).items() if key == "page.use_builder" and granted} \
        == reached
    assert ("page.use_builder" in _keys(conn, "role", 1)) == (builder == "user")


def test_custom_roles_and_individual_permissions_keep_what_they_allowed(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, kanban_access="all", kanban_write_access="editor")
    conn.execute("INSERT INTO custom_roles (id, name, base_role) VALUES (1, 'Members', 'user'), "
                 "(2, 'Writers', 'editor'), (3, 'Blind', 'user')")
    for role_id, keys in ((1, ("page.view_all", "kanban.view", "kanban.create", "canvas.view", "canvas.create")),
                          (2, ("page.view_all", "page.create", "page.delete", "kanban.view", "kanban.create")),
                          (3, ("page.view_all", "kanban.create"))):
        conn.executemany("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                         [(role_id, key) for key in keys])
    conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES ('us1', 'read', 0)")
    conn.executemany("INSERT INTO user_permissions (user_id, permission_key) VALUES ('us1', ?)",
                     [("page.view_all",), ("kanban.view",)])
    conn = _upgrade(tmp_path / "w.db", conn)
    own = {"edit_own", "delete_own", "share_own"}
    # Below the write setting kanban.create did nothing; canvas.create given by choice did.
    assert _keys(conn, "role", 1) == {"page.view_all", "kanban.view", "canvas.view", "canvas.create",
                                      *(f"kanban.{v}" for v in own), *(f"canvas.{v}" for v in own)}
    assert _keys(conn, "role", 2) == {"page.view_all", "page.create", "page.delete_all", "kanban.view",
                                      "kanban.create", "kanban.edit_all", *(f"kanban.{v}" for v in own)}
    assert _keys(conn, "role", 3) == {"page.view_all"}  # nothing worked without kanban.view
    # Individual permissions are kept too: no page creation they did not have.
    assert _keys(conn, "user", "us1") == {"page.view_all", "kanban.view", *(f"kanban.{v}" for v in own)}


def test_editors_who_wrote_everywhere_keep_reading_everywhere(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Docs')")
    for user_id, write in (("ed1", 0), ("ed2", 1)):
        conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1), "
                     "(?, 'write', ?)", (user_id, user_id, write))
        conn.execute("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, 1, 'read')",
                     (user_id,))
    conn.execute("INSERT INTO custom_roles (id, name, base_role, read_restricted, write_restricted) "
                 "VALUES (1, 'E', 'editor', 1, 0), (2, 'U', 'user', 1, 0)")
    conn = _upgrade(tmp_path / "w.db", conn)
    restricted = dict(conn.execute("SELECT user_id, restricted FROM user_category_access WHERE access_type = 'read'"))
    assert restricted == {"ed1": 0, "ed2": 1}  # ed2's writing was limited: its reading limit stays
    assert dict(conn.execute("SELECT id, read_restricted FROM custom_roles")) == {1: 0, 2: 1}


def test_the_mappings_are_documented():
    for name in ("kanban_access", "kanban_write_access", "kanban_open_access", "kanban_public_access_enabled",
                 "canvas_access", "canvas_write_access", "canvas_open_access", "canvas_public_access_enabled",
                 "page_builder_access", "page.delete", "chat_dm_enabled", "chat_allow_dm_creation",
                 "chat_group_enabled", "chat_allow_group_creation", "chat_attachments_enabled",
                 "api_service_enabled"):
        assert name in v23_permissions.__doc__, name


@pytest.mark.parametrize(("switch", "taken"), [
    ("chat_dm_enabled", {"chat.dm", "chat.start_dm"}),
    ("chat_allow_dm_creation", {"chat.start_dm"}),
    ("chat_group_enabled", {"chat.group", "chat.create_group"}),
    ("chat_allow_group_creation", {"chat.create_group"}),
    ("chat_attachments_enabled", {"chat.upload"}),
])
def test_chat_switches_become_permissions(tmp_path, switch, taken):
    conn = _version_22(tmp_path / "w.db")
    _settings(conn, **{switch: 0})
    conn.execute("INSERT INTO custom_roles (id, name, base_role) VALUES (1, 'R', 'user')")
    conn.executemany("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (1, ?)",
                     [("chat.dm",), ("chat.group",), ("chat.create_group",), ("chat.upload",)])
    conn = _upgrade(tmp_path / "w.db", conn)
    chat = {key: granted for key, granted in _role_changes(conn).items() if key[1].startswith("chat.")}
    assert chat == {(role, key): 0 for role in ("user", "editor") for key in taken}
    assert _keys(conn, "role", 1) == {"chat.dm", "chat.start_dm", "chat.group", "chat.create_group",
                                      "chat.upload"} - taken


def test_chat_switches_on_change_nothing_but_add_starting_conversations(tmp_path):
    conn = _version_22(tmp_path / "w.db")
    conn.execute("INSERT INTO custom_roles (id, name, base_role) VALUES (1, 'R', 'user'), (2, 'Quiet', 'user')")
    conn.executemany("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                     [(1, "chat.dm"), (2, "chat.create_group")])
    conn = _upgrade(tmp_path / "w.db", conn)
    assert not [key for key in _role_changes(conn) if key[1].startswith("chat.")]
    assert _keys(conn, "role", 1) == {"chat.dm", "chat.start_dm"}
    assert _keys(conn, "role", 2) == set()  # creating groups never worked without taking part in them


@pytest.mark.parametrize(("plugin", "setting", "expected"), [(1, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 0)])
def test_the_api_keeps_one_switch(tmp_path, plugin, setting, expected):
    conn = _version_22(tmp_path / "w.db")
    conn.execute("INSERT OR REPLACE INTO plugins (id, name, version, builtin, enabled) "
                 "VALUES ('api_service', 'REST API', '1', 1, ?)", (plugin,))
    _settings(conn, api_service_enabled=setting)
    conn = _upgrade(tmp_path / "w.db", conn)
    assert conn.execute("SELECT enabled FROM plugins WHERE id = 'api_service'").fetchone()[0] == expected
