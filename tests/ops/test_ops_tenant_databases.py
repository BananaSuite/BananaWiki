"""A hosted wiki's database is the tenant's: the root controller copies it as bytes and never opens it.

The tenant controls its database and the WAL next to it. Opening them with
SQLite would parse attacker-controlled content in the most privileged process
of the platform. The snapshot copies the
database with its WAL or journal once the wiki is stopped; SQLite replays the
WAL when the wiki opens the restored copy, inside its own sandbox.
"""

from __future__ import annotations

import sqlite3

from bananawiki.ops import profile
from bananawiki.ops import snapshot as snapshot_module
from bananawiki.ops.snapshot import Snapshot


def test_the_controller_never_opens_a_tenant_database(tmp_path, monkeypatch):
    root = tmp_path / "opt/bananawiki"
    tenant = root / "data/instances/acme"
    tenant.mkdir(parents=True)
    with sqlite3.connect(root / "data/hosting.db") as connection:
        connection.execute("CREATE TABLE accounts (id INTEGER)")
    writer = sqlite3.connect(tenant / "bananawiki.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")  # what the wiki committed lives only in its WAL
    writer.execute("CREATE TABLE pages (title TEXT)")
    writer.execute("INSERT INTO pages VALUES ('in the wal')")
    writer.commit()
    assert (tenant / "bananawiki.db-wal").stat().st_size and (tenant / "bananawiki.db-shm").exists()
    opened: list[str] = []
    original = snapshot_module._copy_database

    def copy_database(directory, name, *args, **kwargs):
        opened.append(name)
        return original(directory, name, *args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_copy_database", copy_database)
    snapshot = Snapshot.create(root, lambda path: profile.inside_tenant(path, root / "data"))
    try:
        writer.execute("INSERT INTO pages VALUES ('after the capture')")
        writer.commit()
        snapshot.refresh()  # as after quiesce
        index = snapshot.index()
        assert set(opened) == {"hosting.db"}, "only the platform's own database goes through SQLite"
        assert index["data/hosting.db"]["method"] == "sqlite"
        assert index["data/instances/acme/bananawiki.db"]["method"] == "copy"
        assert index["data/instances/acme/bananawiki.db-wal"]["method"] == "copy"
        assert "data/instances/acme/bananawiki.db-shm" not in index  # rebuilt from the WAL when opened
        restored = tmp_path / "restored"
        snapshot.copy_tree("data", restored)
    finally:
        snapshot.remove()
        writer.close()
    # What the wiki will open in its sandbox after a restore: every committed row, the WAL replayed.
    with sqlite3.connect(restored / "instances/acme/bananawiki.db") as connection:
        assert [row[0] for row in connection.execute("SELECT title FROM pages ORDER BY rowid")] == \
            ["in the wal", "after the capture"]
