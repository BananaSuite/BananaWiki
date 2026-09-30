"""The notification policy shared by the wiki and the portal (bananawiki.core.attention)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bananawiki.core import attention
from bananawiki.core.sqlite import Session, dict_row

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def state(**values):
    return attention.RecipientState(**values)


def test_fresh_sources_need_a_count_and_something_new():
    known = state(counts={"a": 2, "b": 1})
    assert attention.fresh_sources(known, {"a": 3, "b": 1, "c": 0}, set()) == ("a",)
    assert attention.fresh_sources(known, {"a": 2, "b": 1}, {"b", "c"}) == ("b",)
    assert attention.fresh_sources(known, {"a": 1}, set()) == ()


def test_immediate_sends_for_anything_new():
    policy = attention.Policy(mode="immediate")
    assert attention.decide(policy, state(), {"a": 1}, set(), NOW).send
    assert not attention.decide(policy, state(counts={"a": 1}), {"a": 1}, set(), NOW).send
    decision = attention.decide(policy, state(counts={"a": 1}, last_sent_at=NOW), {"a": 1}, {"a"}, NOW)
    assert decision.send and decision.fresh == ("a",)


def test_digest_holds_new_items_until_the_interval_passed():
    policy = attention.Policy(mode="digest", interval_minutes=30)
    recent = state(counts={"a": 1}, last_sent_at=NOW - timedelta(minutes=10))
    held = attention.decide(policy, recent, {"a": 2}, set(), NOW)
    assert not held.send and not held.advance  # kept pending for the next email
    assert attention.decide(policy, recent, {"a": 2}, set(), NOW + timedelta(minutes=21)).send
    quiet = attention.decide(policy, recent, {"a": 0}, set(), NOW)
    assert not quiet.send and quiet.advance


def test_daily_summary_timing_and_time_zone():
    policy = attention.Policy(mode="daily", daily_hour=8, timezone=ZoneInfo("Europe/Rome"))
    morning = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)  # 07:30 in Rome
    assert not attention.decide(policy, state(), {"a": 1}, set(), morning).send
    assert attention.decide(policy, state(), {"a": 1}, set(), morning + timedelta(hours=1)).send
    sent_today = state(last_daily_on="2026-03-01")
    assert not attention.decide(policy, sent_today, {"a": 1}, {"a"}, morning + timedelta(hours=2)).send
    assert not attention.decide(policy, state(), {"a": 0}, set(), morning + timedelta(hours=2)).send


def test_failures_back_off():
    policy = attention.Policy(mode="immediate")
    failed = state(last_failed_at=NOW - timedelta(minutes=5))
    assert not attention.decide(policy, failed, {"a": 1}, set(), NOW).send
    assert attention.decide(policy, failed, {"a": 1}, set(), NOW + timedelta(minutes=11)).send


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = dict_row
    attention.create_tables(conn, "t_events", "t_recipients")
    return attention.Store(Session(conn), "t_events", "t_recipients")


def test_run_is_idempotent_and_saves_after_each_delivery(store):
    policy = attention.Policy(mode="immediate")
    sent = []

    def deliver(key, counts, fresh):
        sent.append((key, fresh))
        return key != "broken"

    recipients = [("ok", {"a": 1}), ("broken", {"a": 1})]
    assert attention.run(store, policy, recipients, deliver, now=NOW) == 1
    assert attention.run(store, policy, recipients, deliver, now=NOW + timedelta(minutes=1)) == 0
    assert sent == [("ok", ("a",)), ("broken", ("a",))]  # the failed one waits for the retry delay
    assert attention.run(store, policy, recipients, deliver, now=NOW + timedelta(minutes=20)) == 0
    assert sent[-1] == ("broken", ("a",))


def test_events_mark_new_items_and_departed_recipients_are_forgotten(store):
    policy = attention.Policy(mode="immediate")
    attention.run(store, policy, [("x", {"a": 1}), ("y", {"a": 1})], lambda *a: True, now=NOW)
    store.record_event("a", 42)
    sent = []
    attention.run(store, policy, [("x", {"a": 1})], lambda key, counts, fresh: sent.append(key) or True,
                  now=NOW + timedelta(minutes=1))
    assert sent == ["x"]
    assert store.load("y") == attention.RecipientState()


def test_poll_due(store):
    assert store.poll_due(NOW)
    store.mark_polled(NOW, store.max_event_id())
    assert not store.poll_due(NOW + timedelta(seconds=10))
    assert store.poll_due(NOW + timedelta(seconds=10), always=True)
    store.record_event("a")
    assert store.poll_due(NOW + timedelta(seconds=10))
    assert store.poll_due(NOW + timedelta(seconds=attention.POLL_SECONDS))


def test_table_names_are_checked():
    with pytest.raises(ValueError):
        attention.Store(None, "events; DROP TABLE users", "r")  # type: ignore[arg-type]
