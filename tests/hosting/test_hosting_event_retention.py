"""The moderation log (``hosting_events``) is pruned after its retention period.

The privacy policy promises that moderation records are deleted at the
latest 10 years after the decision: ``HOSTING_EVENT_RETENTION_DAYS`` (3650 by
default) is how long the maintenance service keeps them.
"""

from __future__ import annotations

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting import instances, maintenance
from bananawiki.hosting.config import load_config

from .hosting_support import build_portal, portal_environ


def _event(query, subject_id: str, action: str, days_ago: int, subject_type: str = "account") -> None:
    query("INSERT INTO hosting_events (subject_type, subject_id, action, actor_id, reason, created_at) "
          "VALUES (?, ?, ?, NULL, '', datetime('now', ?))", (subject_type, subject_id, action, f"-{days_ago} days"))


def _actions(query) -> list[str]:
    return [row["action"] for row in query("SELECT action FROM hosting_events WHERE action LIKE 'test.%' ORDER BY id")]


def test_the_default_keeps_moderation_records_for_ten_years(tmp_path):
    assert load_config(portal_environ(tmp_path), secret_key="k" * 40).event_retention_days == 3650


def test_retention_is_validated(tmp_path):
    assert load_config(portal_environ(tmp_path, HOSTING_EVENT_RETENTION_DAYS="0"),
                       secret_key="k" * 40).event_retention_days == 0
    with pytest.raises(ConfigError):
        load_config(portal_environ(tmp_path, HOSTING_EVENT_RETENTION_DAYS="-1"), secret_key="k" * 40)


def test_maintenance_deletes_entries_older_than_the_retention_period(portal, query):
    _event(query, "a1", "test.ancient", 3651)
    _event(query, "a1", "test.recent", 3649)
    _event(query, "a2", "test.today", 0)
    results = maintenance.run_once(portal)
    assert results["moderation log"] == 1
    assert _actions(query) == ["test.recent", "test.today"]
    assert maintenance.run_once(portal)["moderation log"] == 0


def test_a_shorter_retention_is_honoured(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_EVENT_RETENTION_DAYS="30"))
    from bananawiki.hosting.db import connection_scope

    def query(sql, params=()):
        with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
            return db.all(sql, params) if sql.startswith("SELECT") else db.execute(sql, params)

    _event(query, "a1", "test.old", 31)
    _event(query, "a1", "test.new", 29)
    maintenance.run_once(portal)
    assert _actions(query) == ["test.new"]


def test_zero_keeps_every_entry(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_EVENT_RETENTION_DAYS="0"))
    from bananawiki.hosting.db import connection_scope

    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        db.execute("INSERT INTO hosting_events (subject_type, subject_id, action, reason, created_at) "
                    "VALUES ('account', 'a1', 'test.ancient', '', '2000-01-01 00:00:00')")
    assert maintenance.run_once(portal)["moderation log"] == 0
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT COUNT(*) FROM hosting_events WHERE action = 'test.ancient'") == 1


def test_records_an_unfinished_creation_still_needs_are_kept(portal, ctx, make_account, query):
    from bananawiki.hosting.db import db

    owner = make_account()
    pending = instances._insert(owner, "unfinished", "hosting", admin_username="admin", custom_credentials=False,
                                easy_wiki=False, use_case="")
    db.execute("INSERT INTO hosting_events (subject_type, subject_id, action, reason, created_at) "
               "VALUES ('instance', ?, ?, '', '2000-01-01 00:00:00')", (pending["id"], instances.PROVISIONING_STARTED))
    db.execute("INSERT INTO hosting_events (subject_type, subject_id, action, reason, created_at) "
               "VALUES ('instance', ?, 'instance.created', '', '2000-01-01 00:00:00')", (pending["id"],))
    from bananawiki.hosting import events

    assert events.prune(3650) == 1
    assert db.column("SELECT action FROM hosting_events WHERE subject_id = ?",
                     (pending["id"],)) == [instances.PROVISIONING_STARTED]


def test_a_long_log_with_gaps_is_pruned_exactly_up_to_the_cutoff(portal, ctx):
    from bananawiki.hosting import events
    from bananawiki.hosting.db import db

    db.execute("DELETE FROM hosting_events")
    for day in range(400, 0, -1):  # oldest first, as the log is written
        db.execute("INSERT INTO hosting_events (subject_type, subject_id, action, reason, created_at) "
                   "VALUES ('account', 'a1', 'test.day', ?, datetime('now', ?, '-12 hours'))",
                   (str(day), f"-{day} days"))
    db.execute("DELETE FROM hosting_events WHERE CAST(reason AS INTEGER) % 7 = 0")  # gaps in the ids
    expected_old = db.scalar("SELECT COUNT(*) FROM hosting_events WHERE CAST(reason AS INTEGER) >= 100")
    assert events.prune(100) == expected_old
    assert db.scalar("SELECT MAX(CAST(reason AS INTEGER)) FROM hosting_events") == 99
    assert events.prune(100) == 0
