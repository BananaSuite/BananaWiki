"""The 1.4 takeover of hosting.db repairs every chain of orphaned rows, however long.

Foreign keys are off while a migration runs, so removing an orphan does not
cascade by itself: its own children dangle in turn. The repair repeats until
nothing dangles, as the wiki's takeover does, instead of stopping after a
fixed number of passes and leaving the integrity check to roll the upgrade
back on every start.
"""

from __future__ import annotations

import sqlite3

from bananawiki.hosting.migrations import v3_takeover


def _chain(depth: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE owners (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES nodes(id), "
                 "owner_id TEXT REFERENCES owners(id))")
    conn.execute("INSERT INTO owners VALUES ('kept')")
    # Node 1 points at a parent that is gone; every later node hangs below the previous one.
    conn.execute("INSERT INTO nodes VALUES (1, 999, 'kept')")
    for node in range(2, depth + 1):
        conn.execute("INSERT INTO nodes VALUES (?, ?, 'kept')", (node, node - 1))
    # An unrelated, healthy tree and a nullable reference to a missing owner.
    conn.execute("INSERT INTO nodes VALUES (100, 100, 'kept')")
    conn.execute("INSERT INTO nodes VALUES (101, 100, 'gone')")
    return conn


def test_a_chain_longer_than_eight_levels_is_removed_completely():
    conn = _chain(12)
    v3_takeover.repair_dangling_references(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT id, parent_id, owner_id FROM nodes ORDER BY id").fetchall() == [
        (100, 100, "kept"), (101, 100, None)]


def test_a_clean_database_is_left_alone():
    conn = _chain(1)
    conn.execute("DELETE FROM nodes WHERE id IN (1, 101)")
    v3_takeover.repair_dangling_references(conn)
    assert conn.execute("SELECT id FROM nodes").fetchall() == [(100,)]
