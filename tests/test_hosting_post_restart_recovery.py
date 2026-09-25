"""Tests for task #6: post-restart / post-deploy auto-recovery."""

from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import pytest


# Ensure repo root is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hosting import config as hosting_config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path):
    """Point the hosting DB and instances dir at a temp directory per test."""
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db
    init_hosting_db()
    yield


class TestPostRestartHealthWatch:
    """``_spawn_post_restart_health_watch`` must:

    * Return ``None`` and not spawn anything when the instance has no port.
    * Spawn a daemon thread and stop early once ``/healthz`` returns True.
    * Trigger lazy recovery when ``/healthz`` never returns True in the window.
    """

    def test_no_port_returns_none(self):
        from hosting.instance_manager import _spawn_post_restart_health_watch

        inst = {"subdomain": "noport", "port": None}
        thread = _spawn_post_restart_health_watch(inst, grace_seconds=0.5)
        assert thread is None

    def test_stops_when_instance_becomes_healthy(self):
        from hosting import instance_manager as im

        inst = {"subdomain": "healthy", "port": 65000}
        # Pretend the instance answers /healthz on the very first probe.
        with patch.object(im, "_instance_http_ready", return_value=True) as ready_mock, \
                patch.object(im, "_trigger_lazy_recovery", create=True) as trigger_mock:
            thread = im._spawn_post_restart_health_watch(inst, grace_seconds=2.0)
            assert thread is not None
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            # Healthy → should have returned without escalating.
            assert ready_mock.call_count >= 1
            trigger_mock.assert_not_called()

    def test_triggers_lazy_recovery_when_never_healthy(self):
        from hosting import instance_manager as im
        from hosting import _subdomain_proxy as proxy

        inst = {"subdomain": "wedged", "port": 65000}
        triggered = threading.Event()

        def _fake_trigger(subdomain, domain_mode="hosting"):
            triggered.set()

        with patch.object(im, "_instance_http_ready", return_value=False), \
                patch.object(proxy, "_trigger_lazy_recovery", side_effect=_fake_trigger):
            thread = im._spawn_post_restart_health_watch(
                inst, grace_seconds=0.5,
            )
            assert thread is not None
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert triggered.is_set(), "lazy recovery escalation never fired"


class TestRestartInstanceSpawnsWatch:
    """``restart_instance`` must spawn the auto-recover watch on success."""

    def test_restart_spawns_post_restart_watch(self):
        from hosting import instance_manager as im
        from hosting.db import create_instance, update_instance_status
        from hosting.db._accounts import create_account

        account_id = create_account("alice", "password123")
        inst = create_instance(
            account_id, "wikia",
            admin_username="admin", admin_password="password123",
        )
        # Move the instance into the ``stopped`` state so ``restart_instance``
        # actually exercises the start codepath.
        update_instance_status(inst["id"], "stopped")

        with patch.object(im, "_start_process", return_value=True), \
                patch.object(im, "_spawn_post_restart_health_watch") as watch_mock:
            ok, reason = im.restart_instance(inst["id"])
            assert ok is True, reason
            watch_mock.assert_called_once()
