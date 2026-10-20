"""The copy of the database saved before a schema upgrade never keeps a hosted wiki from starting.

A hosted wiki's container sees only its own data directory, which is under
the hard storage quota of its plan, and the platform's update already keeps a
package of every wiki from before the update. A copy written there would only
take the owner's room, permanently, and fail the start of a wiki with less
room left than its database. Hosted wikis therefore write none. A standalone
wiki writes one; when it cannot (a full disk), the start is refused
with a message that says where and why, and nothing is upgraded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_core_migrations import copies, instance, make_db

from bananawiki.wiki import migrations, takeover


def test_a_hosted_wiki_is_upgraded_without_a_copy_inside_its_quota(tmp_path):
    standalone, path = instance(tmp_path)
    cfg = SimpleNamespace(instance_dir=standalone.instance_dir, managed_hosting=True)
    db = make_db(path)
    db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, conn))
    assert db.version()[1] == migrations.LATEST
    assert copies(cfg) == [] and not (Path(cfg.instance_dir) / "backups").exists()


class FullDisk:
    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def backup(self, target, **_options):
        raise sqlite3.OperationalError("database or disk is full")


def test_a_copy_that_does_not_fit_refuses_the_start_with_a_clear_message(tmp_path):
    cfg, path = instance(tmp_path)
    db = make_db(path)
    with pytest.raises(RuntimeError, match="could not be saved") as error:
        db.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, db, FullDisk(conn)))
    message = str(error.value)
    assert "backups" in message and "free space" in message and "nothing was upgraded" in message.lower()
    assert db.version()[1] == 3
    assert list((Path(cfg.instance_dir) / "backups").iterdir()) == []
