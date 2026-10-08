# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Personal data export: no database read stays open while the archive downloads."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile


def test_the_wal_can_be_checkpointed_between_every_chunk_of_the_download(app, client, make_user, login, db):
    bob = make_user("bob")
    db.execute("CREATE TABLE wal_probe (n INTEGER)")
    db.executemany("INSERT INTO username_history (user_id, old_username, new_username, changed_at) "
                   "VALUES (?, ?, ?, '2026-01-01 00:00:00')",
                   [(bob["id"], f"bob_{n}", f"bob_{n + 1}") for n in range(450)])
    login(client, bob)
    # Another process: it writes, then needs every reader gone to empty the WAL.
    other = sqlite3.connect(app.extensions["bananawiki.database"].path, timeout=0, isolation_level=None)
    blocked, body = [], []
    try:
        response = client.get("/settings/export")
        assert response.is_streamed
        for chunk in response.response:
            body.append(chunk)
            other.execute("INSERT INTO wal_probe VALUES (1)")
            blocked.append(other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
        response.close()
    finally:
        other.close()
    assert len(blocked) > 5
    assert not any(blocked), "a read stayed open while the client was downloading"
    archive = zipfile.ZipFile(io.BytesIO(b"".join(body)))
    assert len(json.loads(archive.read("data/username_history.json"))) == 450
    assert json.loads(archive.read("summary.json"))["rows"]["data/username_history.json"] == 450


def test_large_tables_go_through_the_spool_intact(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.user_data_export import service

    monkeypatch.setattr(service, "SPOOL", 1024)  # move to the temporary file almost at once
    bob = make_user("bob")
    db.executemany("INSERT INTO username_history (user_id, old_username, new_username, changed_at) "
                   "VALUES (?, ?, ?, '2026-01-01 00:00:00')",
                   [(bob["id"], f"bob_{n}", f"bob_{n + 1}") for n in range(3000)])
    login(client, bob)
    response = client.get("/settings/export")
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    assert archive.testzip() is None
    rows = json.loads(archive.read("data/username_history.json"))
    assert [row["old_username"] for row in rows] == [f"bob_{n}" for n in range(3000)]


def test_a_download_left_halfway_closes_its_spool(app, client, make_user, login, monkeypatch):
    from bananawiki.wiki.features.user_data_export import service

    sinks = []

    class Sink(service._Sink):
        def __init__(self) -> None:
            super().__init__()
            sinks.append(self)

    monkeypatch.setattr(service, "_Sink", Sink)
    login(client, make_user("bob"))
    response = client.get("/settings/export")
    assert next(iter(response.response))
    response.close()  # the client went away
    assert len(sinks) == 1
    assert sinks[0]._spool.closed, "the spool waited for the garbage collector"
