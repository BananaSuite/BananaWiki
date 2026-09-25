"""Pause and resume cannot undo an administrator's suspension.

Customer paths never move a wiki out of ``suspended``, a stale suspension
marker blocks resuming, and the final status writes of stop and restart are
compare-and-set, so a suspension that lands halfway through wins.
"""

from datetime import datetime, timezone

import pytest

from helpers._passwords import generate_password_hash
from hosting import config as hosting_config
from hosting import instance_manager
from hosting.app import create_hosting_app
from hosting.db import (
    add_collaborator,
    create_account,
    create_instance,
    get_hosting_db_context,
    get_instance,
    init_hosting_db,
    update_instance_status,
)

PASSWORD = "password123"


@pytest.fixture(autouse=True)
def isolated_hosting_db(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    monkeypatch.setattr(hosting_config, "INSTANCES_DIR", str(tmp_path / "instances"))
    init_hosting_db()


@pytest.fixture
def client():
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application.test_client()


@pytest.fixture
def processes(monkeypatch):
    """Record process starts and stops instead of running gunicorn."""
    calls = []

    def fake_start(inst, data_dir, **kwargs):
        calls.append(("start", inst["id"]))
        return True

    def fake_stop(data_dir, timeout=15, port=None):
        calls.append(("stop", data_dir))

    monkeypatch.setattr(instance_manager, "_start_process", fake_start)
    monkeypatch.setattr(instance_manager, "_stop_process", fake_stop)
    monkeypatch.setattr(instance_manager, "_clean_stale_pid", lambda *args, **kwargs: None)
    monkeypatch.setattr(instance_manager, "_spawn_post_restart_health_watch", lambda *args, **kwargs: None)
    return calls


def _account(username):
    return create_account(username, generate_password_hash(PASSWORD))


def _login(client, username):
    response = client.post("/login", data={"username": username, "password": PASSWORD})
    assert response.status_code == 302, response.get_data(as_text=True)


def _instance(account_id, subdomain):
    return create_instance(account_id, subdomain, "wikiadmin", "one-time-secret")


def _mark_suspended_at(instance_id):
    with get_hosting_db_context() as conn:
        conn.execute("UPDATE instances SET suspended_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), instance_id))
        conn.commit()


def test_a_collaborator_cannot_pause_a_suspended_wiki(client, processes):
    owner = _account("owner")
    operator = _account("operator")
    inst = _instance(owner, "frozenwiki")
    add_collaborator(inst["id"], operator, owner, permissions=["view", "start_stop"])
    instance_manager.suspend_instance(inst["id"])
    processes.clear()
    _login(client, "operator")

    response = client.post(f"/instances/{inst['id']}/stop")
    assert response.status_code == 302
    assert get_instance(inst["id"])["status"] == "suspended"
    assert processes == []


def test_only_admin_paths_stop_a_suspended_wiki(processes):
    owner = _account("owner")
    inst = _instance(owner, "adminwiki")
    update_instance_status(inst["id"], "suspended")

    assert instance_manager.stop_instance(inst["id"]) == (False, "Instance is suspended.")
    assert get_instance(inst["id"])["status"] == "suspended"
    assert processes == []

    assert instance_manager.stop_instance(inst["id"], allow_suspended=True) == (True, "")
    assert get_instance(inst["id"])["status"] == "stopped"


def test_a_suspension_marker_blocks_every_customer_resume(client, processes):
    owner = _account("owner")
    inst = _instance(owner, "markedwiki")
    update_instance_status(inst["id"], "stopped")
    _mark_suspended_at(inst["id"])

    ok, reason = instance_manager.restart_instance(inst["id"])
    assert not ok and "suspended" in reason
    _login(client, "owner")
    client.post(f"/instances/{inst['id']}/restart")
    assert get_instance(inst["id"])["status"] == "stopped"
    assert processes == []

    assert instance_manager.restart_instance(inst["id"], allow_suspended=True) == (True, "")
    assert get_instance(inst["id"])["status"] == "running"


def test_unsuspending_still_restarts_the_wiki(processes):
    owner = _account("owner")
    inst = _instance(owner, "backwiki")
    instance_manager.suspend_instance(inst["id"])

    assert instance_manager.unsuspend_instance(inst["id"]) == (True, "")
    row = get_instance(inst["id"])
    assert row["status"] == "running"
    assert not row["suspended_at"]


def test_a_suspension_during_a_resume_wins(processes, monkeypatch):
    owner = _account("owner")
    inst = _instance(owner, "racewiki")
    update_instance_status(inst["id"], "stopped")

    def start_while_suspended(started_inst, data_dir, **kwargs):
        processes.append(("start", started_inst["id"]))
        update_instance_status(started_inst["id"], "suspended")
        return True

    monkeypatch.setattr(instance_manager, "_start_process", start_while_suspended)
    ok, reason = instance_manager.restart_instance(inst["id"])
    assert not ok and "changed state" in reason
    assert get_instance(inst["id"])["status"] == "suspended"
    # The process that was started is stopped again.
    assert [call[0] for call in processes] == ["start", "stop"]


def test_a_suspension_during_a_pause_is_kept(processes, monkeypatch):
    owner = _account("owner")
    inst = _instance(owner, "pausewiki")

    def stop_while_suspended(data_dir, timeout=15, port=None):
        update_instance_status(inst["id"], "suspended")

    monkeypatch.setattr(instance_manager, "_stop_process", stop_while_suspended)
    ok, reason = instance_manager.stop_instance(inst["id"])
    assert not ok and "changed state" in reason
    assert get_instance(inst["id"])["status"] == "suspended"


def test_suspending_marks_the_row_before_stopping_the_process(processes, monkeypatch):
    owner = _account("owner")
    inst = _instance(owner, "orderwiki")
    update_instance_status(inst["id"], "stopped")
    seen = []

    def record_status(data_dir, timeout=15, port=None):
        seen.append(get_instance(inst["id"])["status"])

    monkeypatch.setattr(instance_manager, "_stop_process", record_status)
    assert instance_manager.suspend_instance(inst["id"]) == (True, "")
    # Stopped even though the row said stopped, in case a resume was starting it.
    assert seen == ["suspended"]
    assert get_instance(inst["id"])["suspended_at"]
