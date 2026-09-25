"""Tests for reliability tooling and observability instrumentation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import db
from ops_observability import (
    get_observability_snapshot,
    record_http_error,
    reset_observability_snapshot,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_build_incident_timeline_cli(tmp_path):
    """Timeline script should detect 500/502/504 and deploy markers."""
    log = tmp_path / "sample.log"
    log.write_text(
        "\n".join(
            [
                '2026-05-17T10:00:00+00:00 GET / 500 "..."',
                '2026-05-17T10:01:00+00:00 deploy.sh update complete',
                '2026-05-17T10:02:00+00:00 GET / 502 "..."',
                '2026-05-17T10:03:00+00:00 GET / 504 "..."',
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "timeline.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "scripts" / "build_incident_timeline.py"),
            "--log",
            str(log),
            "--output",
            str(out),
        ],
        cwd=str(_repo_root()),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["http_500"]["count"] == 1
    assert payload["summary"]["http_502"]["count"] == 1
    assert payload["summary"]["http_504"]["count"] == 1
    assert payload["summary"]["deploy_or_restart"]["count"] == 1


def test_validate_runtime_baseline_cli_passes_on_repo_files():
    """Baseline validator should pass against repository hardened defaults."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_repo_root() / "scripts" / "validate_runtime_baseline.py"),
            "--repo-root",
            str(_repo_root()),
        ],
        cwd=str(_repo_root()),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def test_retry_on_busy_records_observability(monkeypatch):
    """Retry wrapper should populate retry observability counters on recovery."""
    reset_observability_snapshot()
    from db import _connection
    import sqlite3

    monkeypatch.setattr(_connection, "_BUSY_RETRY_BASE_SLEEP", 0.0)

    calls = {"n": 0}

    @db.retry_on_busy
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert flaky() == "ok"
    snap = db.get_db_observability_snapshot()
    assert snap["counters"]["sqlite_retry_events_total"] >= 1


def test_http_error_counter_snapshot():
    """HTTP error recording should increment structured counters."""
    reset_observability_snapshot()
    record_http_error("wiki", 500, route="/demo")
    snap = get_observability_snapshot()
    assert snap["counters"]["http_error_total"] == 1
    assert snap["counters"]["http_error_500_total"] == 1
    assert snap["counters"]["http_error_surface:wiki:status:500"] == 1


def test_http_error_counter_caps_high_cardinality_routes():
    """High-cardinality routes should collapse to <overflow> past the cap."""
    from ops_observability import _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET

    reset_observability_snapshot()
    cap = _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET
    extra = 8
    for i in range(cap + extra):
        record_http_error("wiki", 500, route=f"/page/{i}")

    snap = get_observability_snapshot()
    counters = snap["counters"]
    assert counters["http_error_total"] == cap + extra
    assert counters["http_error_500_total"] == cap + extra
    # Bounded route key keys: exactly `cap` unique route counters, plus a
    # single <overflow> key counting the remaining errors.
    route_keys = [
        k for k in counters
        if k.startswith("http_error_surface:wiki:status:500:route:")
    ]
    assert len(route_keys) == cap + 1
    assert "http_error_surface:wiki:status:500:route:<overflow>" in counters
    assert counters["http_error_surface:wiki:status:500:route:<overflow>"] == extra
    assert counters["http_error_surface:wiki:status:500:route_overflow_total"] == extra


def test_http_error_counter_caps_high_cardinality_upstreams():
    """High-cardinality upstream values should also collapse to <overflow>."""
    from ops_observability import _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET

    reset_observability_snapshot()
    cap = _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET
    extra = 4
    for i in range(cap + extra):
        record_http_error(
            "hosting_proxy_upstream",
            502,
            route="<proxy>",
            upstream=f"127.0.0.1:{6000 + i}",
        )

    snap = get_observability_snapshot()
    counters = snap["counters"]
    upstream_keys = [
        k for k in counters
        if k.startswith("http_error_surface:hosting_proxy_upstream:status:502:upstream:")
    ]
    assert len(upstream_keys) == cap + 1
    assert (
        "http_error_surface:hosting_proxy_upstream:status:502:upstream:<overflow>"
        in counters
    )
    assert (
        counters["http_error_surface:hosting_proxy_upstream:status:502:upstream_overflow_total"]
        == extra
    )
