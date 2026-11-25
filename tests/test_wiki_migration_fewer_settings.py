"""Schema version 24: the settings folded into another one keep doing what they did."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bananawiki.core.sqlite import Database
from bananawiki.wiki import migrations


def _database(path: Path, upto: int | None = None) -> Database:
    chain = migrations.MIGRATIONS if upto is None else migrations.MIGRATIONS[: upto - migrations.BASELINE]
    return Database(path, application_id=migrations.APPLICATION_ID, migrations=chain,
                    baseline=migrations.BASELINE, bootstrap=migrations.bootstrap)


def _upgrade(tmp_path: Path, **values) -> dict:
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path)
    conn.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO site_settings (id, setup_done) VALUES (1, 1)")
    conn.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    _database(path, upto=23).initialize()
    conn = sqlite3.connect(path)
    for column, value in values.items():
        conn.execute(f"UPDATE site_settings SET {column} = ?", (value,))
    conn.commit()
    conn.close()
    assert _database(path).initialize() == migrations.LATEST - 23
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return dict(conn.execute("SELECT * FROM site_settings").fetchone())


def test_a_hidden_public_notice_stays_hidden(tmp_path):
    assert _upgrade(tmp_path, public_mode_message="Hi", public_mode_show_message=0)["public_mode_message"] == ""


def test_a_shown_public_notice_stays(tmp_path):
    assert _upgrade(tmp_path, public_mode_message="Hi", public_mode_show_message=1)["public_mode_message"] == "Hi"


@pytest.mark.parametrize(("enabled", "url"), [(0, ""), (1, "http://gpu.lan:8787")])
def test_a_gpu_server_switched_off_is_forgotten(tmp_path, enabled, url):
    row = _upgrade(tmp_path, tts_gpu_enabled=enabled, tts_gpu_url="http://gpu.lan:8787")
    assert row["tts_gpu_url"] == url


@pytest.mark.parametrize(("values", "expected"), [
    ({}, (0, 0, 0, 0)),  # cleanup was off: nothing deleted
    ({"chat_cleanup_enabled": 1, "chat_cleanup_split_configured": 1, "chat_dm_auto_clear_messages": 1,
      "chat_dm_message_retention_days": 30, "chat_group_auto_clear_attachments": 1,
      "chat_group_attachment_retention_days": 7, "chat_dm_attachment_retention_days": 9},
     (30, 0, 0, 7)),  # an unticked switch kept that content
    ({"chat_cleanup_enabled": 1, "chat_auto_clear_messages": 1, "chat_message_retention_days": 20},
     (20, 0, 20, 0)),  # the 1.4 single policy (its files were kept)
])
def test_chat_retention_becomes_four_numbers(tmp_path, values, expected):
    row = _upgrade(tmp_path, **values)
    assert tuple(row[f"chat_{scope}_{kind}_retention_days"] for scope in ("dm", "group")
                 for kind in ("message", "attachment")) == expected


def test_bm_mode_keeps_the_features_shown_and_drops_the_simplification_choices(tmp_path):
    choices = {"feature:chat": True, "simplify:pages.simple_editor": False}
    row = _upgrade(tmp_path, bm_choices=json.dumps(choices))
    assert json.loads(row["bm_choices"]) == {"feature:chat": True}
