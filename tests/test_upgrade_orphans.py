"""The 1.4 -> 1.6 takeover repairs chains of orphaned rows instead of refusing the upgrade.

1.4 ran with foreign keys switched off and wrote ``-1`` ("system") into user
columns, so a 1.4 database can hold a row whose required parent is gone and
that still has children of its own. Removing that row must remove its
children too, as ``ON DELETE CASCADE`` would have done, or the integrity
check before COMMIT rolls the whole upgrade back on every start.
"""

import sqlite3

from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations


def _make_db(path):
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=migrations.MIGRATIONS,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _build_v3_with_orphan_chains(path):
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u1', 'alice', 'x', 'owner')")
    conn.execute("PRAGMA foreign_keys=OFF")
    # Two levels: a chat with the "system" user, holding a message from a real user.
    conn.execute("INSERT INTO chats (id, user1_id, user2_id) VALUES (1, 'u1', '-1')")
    conn.execute("INSERT INTO chat_messages (id, chat_id, sender_id, content) VALUES (1, 1, 'u1', 'hi')")
    # Three levels plus nullable references: board -> column -> ticket -> comment and history.
    conn.execute("INSERT INTO kanban_boards (id, title, created_by) VALUES (1, 'Board', '-1')")
    conn.execute("INSERT INTO kanban_columns (id, board_id, title) VALUES (1, 1, 'Todo')")
    conn.execute("INSERT INTO kanban_tickets (id, column_id, title, created_by, assigned_to) "
                 "VALUES (1, 1, 'Ticket', 'u1', 'u1')")
    conn.execute("INSERT INTO kanban_ticket_comments (id, ticket_id, user_id, content) VALUES (1, 1, 'u1', 'ok')")
    conn.execute("INSERT INTO kanban_ticket_history (id, ticket_id, changed_by) VALUES (1, 1, 'u1')")
    # Unrelated data of the same user survives.
    conn.execute("INSERT INTO kanban_boards (id, title, created_by) VALUES (2, 'Kept', 'u1')")
    conn.execute("INSERT INTO kanban_columns (id, board_id, title) VALUES (2, 2, 'Kept column')")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()


def test_orphan_chains_are_removed_and_the_upgrade_completes(tmp_path):
    path = tmp_path / "wiki.db"
    _build_v3_with_orphan_chains(path)
    db = _make_db(path)

    assert db.initialize() == len(migrations.MIGRATIONS)

    conn = db.connect()
    assert db.version() == (migrations.APPLICATION_ID, migrations.LATEST)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def ids(table):
        return [row["id"] for row in conn.execute(f"SELECT id FROM {table} ORDER BY id")]

    assert ids("chats") == [] and ids("chat_messages") == []
    assert ids("kanban_boards") == [2] and ids("kanban_columns") == [2]
    assert ids("kanban_tickets") == [] and ids("kanban_ticket_comments") == []
    assert ids("kanban_ticket_history") == []
    assert conn.execute("SELECT username FROM users").fetchall() == [{"username": "alice"}]
