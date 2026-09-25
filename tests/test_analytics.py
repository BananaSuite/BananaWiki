"""Tests for the per-wiki request analytics module (``db._analytics``)."""

import sqlite3
from datetime import datetime, timezone

import db


def test_record_request_increments_daily_counter():
    db.reset_analytics()
    db.record_request("request")
    db.record_request("request")
    db.record_request("page_view")

    summary = db.get_analytics_summary(days=7)
    assert summary["window_days"] == 7
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_row = next(d for d in summary["daily"] if d["day"] == today)
    assert today_row["request"] == 2
    assert today_row["page_view"] == 1
    assert today_row["error"] == 0

    assert summary["totals"]["request"] == 2
    assert summary["totals"]["page_view"] == 1
    assert summary["totals"]["error"] == 0


def test_record_request_ignores_unknown_kinds():
    db.reset_analytics()
    db.record_request("bogus_kind")
    summary = db.get_analytics_summary(days=1)
    assert summary["totals"]["request"] == 0
    assert summary["totals"]["page_view"] == 0


def test_get_analytics_summary_pads_missing_days():
    db.reset_analytics()
    summary = db.get_analytics_summary(days=14)
    assert len(summary["daily"]) == 14
    for entry in summary["daily"]:
        assert entry["request"] == 0
        assert entry["page_view"] == 0
        assert entry["error"] == 0


def test_get_analytics_summary_supports_external_conn(tmp_path):
    """The hosting portal opens another wiki's DB read-only and queries it."""
    db.reset_analytics()
    for _ in range(5):
        db.record_request("request")

    # Copy the wiki DB to a separate file (simulating a hosted instance
    # that the portal is inspecting).
    import config
    source = config.DATABASE_PATH
    target = str(tmp_path / "snapshot.db")

    # Use SQLite's online backup API so we get a consistent snapshot
    # without holding a write lock on the source connection.
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)

    # Read it back through ``get_analytics_summary`` and the new conn.
    with sqlite3.connect(target) as ro:
        summary = db.get_analytics_summary(days=30, conn=ro)
    assert summary["totals"]["request"] == 5


def test_get_analytics_summary_clamps_days_argument():
    db.reset_analytics()
    s = db.get_analytics_summary(days=0)
    assert s["window_days"] == 1
    s = db.get_analytics_summary(days=10_000)
    assert s["window_days"] == 365


def test_wiki_request_increments_analytics(client, logged_in_admin):
    """End-to-end: a real HTTP request bumps the analytics counter."""
    db.reset_analytics()
    resp = logged_in_admin.get("/")
    assert resp.status_code in (200, 302)

    summary = db.get_analytics_summary(days=1)
    assert summary["totals"]["request"] >= 1


def test_health_probes_do_not_increment_analytics(client):
    """Operational /health and /healthz probes must not count as traffic."""
    db.reset_analytics()
    client.get("/health")
    client.get("/healthz")

    summary = db.get_analytics_summary(days=1)
    assert summary["totals"]["request"] == 0
    assert summary["totals"]["page_view"] == 0
