"""Schema version 26: people's own permission sets become changes to their role, and nobody's access changes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bananawiki.core.sqlite import Database, Session, dict_row
from bananawiki.wiki import migrations
from bananawiki.wiki import permissions as perms


def _database(path: Path, upto: int | None = None) -> Database:
    chain = migrations.MIGRATIONS if upto is None else migrations.MIGRATIONS[: upto - migrations.BASELINE]
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=chain,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _at_25(tmp_path: Path) -> Path:
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    _database(path, upto=25).initialize()
    return path


def _v25_grants(session: Session, user: dict) -> perms.Grants:
    """How version 25 resolved an account (its ``load_grants``, frozen here for the comparison)."""
    role = user.get("role") or "user"
    if role in perms.ADMIN_ROLES:
        return perms.Grants(role, frozenset(perms.CATALOGUE), perms.CategoryAccess(), perms.CategoryAccess())
    if user.get("custom_role_id"):
        custom = session.one("SELECT * FROM custom_roles WHERE id = ?", (user["custom_role_id"],))
        if custom:
            keys = session.column("SELECT permission_key FROM custom_role_permissions WHERE role_id = ?",
                                  (custom["id"],))
            cats = session.all("SELECT category_id, access_type FROM custom_role_categories WHERE role_id = ?",
                               (custom["id"],))
            read_ids = {c["category_id"] for c in cats if c["access_type"] == "read"}
            write_ids = {c["category_id"] for c in cats if c["access_type"] == "write"}
            read_restricted = bool(custom["read_restricted"])
            if custom["write_restricted"]:
                read_restricted = True
                read_ids |= write_ids
            return perms.Grants(role, frozenset(perms.sanitize(role, keys)),
                                perms.CategoryAccess(read_restricted, frozenset(read_ids)),
                                perms.CategoryAccess(bool(custom["write_restricted"]), frozenset(write_ids)))
        return perms.Grants(role, perms.role_defaults(session, role), perms.CategoryAccess(), perms.CategoryAccess())
    rows = session.all("SELECT access_type, restricted FROM user_category_access WHERE user_id = ?", (user["id"],))
    if not rows:
        return perms.Grants(role, perms.role_defaults(session, role), perms.CategoryAccess(), perms.CategoryAccess())
    keys = session.column("SELECT permission_key FROM user_permissions WHERE user_id = ?", (user["id"],))
    restricted = {row["access_type"]: bool(row["restricted"]) for row in rows}
    cats = session.all("SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?",
                       (user["id"],))
    read_ids = frozenset(c["category_id"] for c in cats if c["access_type"] == "read")
    write_ids = frozenset(c["category_id"] for c in cats if c["access_type"] == "write")
    return perms.Grants(role, frozenset(perms.sanitize(role, keys)),
                        perms.CategoryAccess(restricted.get("read", False), read_ids | write_ids),
                        perms.CategoryAccess(restricted.get("write", False), write_ids))


def _effective(grants: perms.Grants, category_ids: list[int | None]) -> tuple:
    """What an account may do: its keys, and the categories it reads and writes (None: no category)."""
    return (grants.keys,
            tuple(perms.can_read_category(grants, c) for c in category_ids),
            tuple(perms.can_write_category(grants, c) for c in category_ids))


def _fixture(conn: sqlite3.Connection) -> None:
    """Every way an account's access was decided at version 25, and rows that version never read."""
    for cid, name in ((1, "Docs"), (2, "Team"), (3, "Archive")):
        conn.execute("INSERT INTO categories (id, name) VALUES (?, ?)", (cid, name))
    # The wiki changed the base roles' defaults.
    conn.executemany("INSERT INTO base_role_permissions (role, permission_key, granted) VALUES (?, ?, ?)",
                     [("user", "page.edit_all", 1), ("user", "chat.dm", 0), ("editor", "page.edit_metadata", 0)])

    def user(user_id, role="user", custom_role_id=None):
        conn.execute("INSERT INTO users (id, username, password, role, custom_role_id) VALUES (?, ?, 'x', ?, ?)",
                     (user_id, user_id, role, custom_role_id))

    def own(user_id, keys, read=None, write=None, read_ids=(), write_ids=()):
        conn.executemany("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                         [(user_id, key) for key in keys])
        for kind, restricted in (("read", read), ("write", write)):
            if restricted is not None:
                conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, ?)",
                             (user_id, kind, int(restricted)))
        conn.executemany("INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, ?)",
                         [(user_id, c, "read") for c in read_ids] + [(user_id, c, "write") for c in write_ids])

    user("plain")
    user("plain_editor", "editor")
    # Own sets: fewer keys than the defaults, keys without the keys they imply, a renamed key,
    # editor-only keys a member may not hold, an unknown key, category lists.
    user("own_member")
    own("own_member", ["page.edit_all", "page.delete", "history.revert", "chat.start_dm", "no.such_key"],
        read=False, write=False)
    user("own_editor", "editor")
    own("own_editor", ["page.view_all", "page.edit_all", "category.create", "kanban.view"],
        read=True, write=True, read_ids=[1], write_ids=[2])
    user("own_writer")
    own("own_writer", sorted(perms.builtin_defaults("user")), write=True, write_ids=[1, 3])
    user("own_reader", "editor")
    own("own_reader", sorted(perms.builtin_defaults("editor")), read=True, read_ids=[3])
    user("empty_set")
    own("empty_set", [], read=False)
    # Rows version 25 never read.
    user("stale_keys")
    own("stale_keys", ["page.delete_all", "history.view"], read_ids=[1])
    conn.execute("INSERT INTO custom_roles (id, name, base_role, read_restricted, write_restricted) "
                 "VALUES (7, 'Writers', 'editor', 0, 1)")
    conn.executemany("INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (7, ?)",
                     [("page.view_all",), ("page.edit_all",)])
    conn.execute("INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (7, 2, 'write')")
    user("roled", "editor", 7)
    own("roled", ["page.delete_all", "chat.dm"], read=True, write=False, read_ids=[1])
    user("boss", "admin")
    own("boss", ["page.view_all"], read=True, read_ids=[1])


def test_everyones_access_stays_exactly_the_same(tmp_path):
    path = _at_25(tmp_path)
    conn = sqlite3.connect(path)
    _fixture(conn)
    conn.commit()
    conn.close()
    categories = [1, 2, 3, None]

    def resolve(resolver) -> dict:
        conn = sqlite3.connect(path)
        conn.row_factory = dict_row
        session = Session(conn)
        found = {row["id"]: _effective(resolver(session, row), categories)
                 for row in session.all("SELECT * FROM users ORDER BY id")}
        conn.close()
        return found

    before = resolve(_v25_grants)
    assert _database(path).initialize() == migrations.LATEST - 25
    after = resolve(perms.load_grants)
    assert after == before
    # The fixture covers what it means to: some own sets differ from the defaults, some categories are limited.
    assert before["own_member"][0] != before["plain"][0]
    assert before["own_editor"][1] == (True, True, False, False)
    assert before["roled"][2] == (False, True, False, False)

    conn = sqlite3.connect(path)
    rows = dict(conn.execute("SELECT user_id, COUNT(*) FROM user_permissions GROUP BY user_id").fetchall())
    # Own sets became a change to every key; rows nobody read are gone.
    assert set(rows) == {"own_member", "own_editor", "own_writer", "own_reader", "empty_set"}
    assert set(rows.values()) == {len(perms.CATALOGUE)}
    others = {"roled", "boss"}
    for table in ("user_category_access", "user_allowed_categories"):
        assert not {r for (r,) in conn.execute(f"SELECT DISTINCT user_id FROM {table}")} & others


def test_people_with_their_own_sets_keep_them_when_defaults_change_later(tmp_path):
    path = _at_25(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('frozen', 'frozen', 'x', 'user')")
    conn.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES ('frozen', 'read', 0)")
    conn.executemany("INSERT INTO user_permissions (user_id, permission_key) VALUES ('frozen', ?)",
                     [("page.view_all",)])
    conn.commit()
    conn.close()
    _database(path).initialize()
    conn = sqlite3.connect(path)
    conn.row_factory = dict_row
    session = Session(conn)
    session.execute("INSERT INTO base_role_permissions (role, permission_key, granted) "
                    "VALUES ('user', 'page.export_pdf', 1)")
    frozen = session.one("SELECT * FROM users WHERE id = 'frozen'")
    assert perms.load_grants(session, frozen).keys == {"page.view_all"}


def test_new_and_upgraded_wikis_have_the_same_schema(tmp_path):
    for path in (_at_25(tmp_path), tmp_path / "new.db"):
        _database(path).initialize()
        conn = sqlite3.connect(path)
        assert "granted" in {row[1] for row in conn.execute("PRAGMA table_info(user_permissions)")}
        assert {"id", "user_id", "scope", "category_id", "page_id", "message", "status", "created_at",
                "decided_by", "decided_at"} == {row[1] for row in conn.execute("PRAGMA table_info(access_requests)")}
        assert conn.execute("SELECT access_requests_enabled FROM site_settings").fetchone()[0] == 0
        conn.close()
