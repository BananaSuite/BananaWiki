"""Merges preserve every tenant and never partially transfer account identities."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from hosting import config as hosting_config
from hosting import db


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    db.init_hosting_db()
    admin = db.create_account("merge-admin", "unused", is_admin=True)
    source = db.create_account("merge-source", "unused")
    target = db.create_account("merge-target", "unused")
    other = db.create_account("merge-other", "unused")
    tenants = [db.create_instance(source, "merge-one"), db.create_instance(source, "merge-two")]
    db.update_instance_status(tenants[0]["id"], "running")
    db.update_instance_status(tenants[1]["id"], "terminated")
    return admin, source, target, other, tenants


def test_account_merge_moves_active_and_retained_tenants_once(accounts):
    admin, source, target, _, tenants = accounts
    version = db.get_account_by_id(source)["session_version"]
    request = db.create_merge_request(source, target, source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(db.approve_by_source, request["id"], source)
        right = pool.submit(db.approve_by_target, request["id"], target)
        left.result()
        right.result()
    assert db.get_merge_request(request["id"])["status"] == "approved"
    result = db.execute_merge(request["id"], admin)
    assert result["instances_transferred"] == 2
    assert [db.get_instance(t["id"])["account_id"] for t in tenants] == [target, target]
    assert [db.get_instance(t["id"])["status"] for t in tenants] == ["running", "terminated"]
    assert db.get_account_by_id(source)["suspended"] == 1
    assert db.get_account_by_id(source)["session_version"] == version + 1
    assert db.get_merge_request(request["id"])["status"] == "merged"
    with pytest.raises(ValueError):
        db.execute_merge(request["id"], admin)


def test_failed_transfer_rolls_back_tenants_and_account(accounts):
    admin, source, target, _, tenants = accounts
    with db.get_hosting_db_context() as conn:
        conn.execute("CREATE TRIGGER reject_merge BEFORE UPDATE OF account_id ON instances "
                     "WHEN OLD.subdomain='merge-two' BEGIN SELECT RAISE(ABORT,'fixture transfer failure'); END")
        conn.commit()
    with pytest.raises(ValueError, match="Nothing was merged"):
        db.admin_execute_merge(source, target, admin)
    assert all(db.get_instance(t["id"])["account_id"] == source for t in tenants)
    assert db.get_account_by_id(source)["suspended"] == 0
    with db.get_hosting_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM hosting_account_merge_logs").fetchone()[0] == 0


def test_conflicting_sso_identity_preserves_original_owners(accounts):
    admin, source, target, _, tenants = accounts
    with db.get_hosting_db_context() as conn:
        for account, wiki_user in ((source, "wiki-user-one"), (target, "wiki-user-two")):
            conn.execute("INSERT INTO hosting_oauth_account_links(instance_id,account_id,wiki_user_id,wiki_username) VALUES(?,?,?,?)",
                         (tenants[0]["id"], account, wiki_user, wiki_user))
        conn.commit()
    with pytest.raises(ValueError, match="different users"):
        db.admin_execute_merge(source, target, admin)
    assert all(db.get_instance(t["id"])["account_id"] == source for t in tenants)
    assert db.get_account_by_id(source)["suspended"] == 0


def test_outsider_cannot_approve_cancel_or_execute_a_merge(accounts):
    admin, source, target, other, _ = accounts
    request = db.create_merge_request(source, target, source)
    for function in (db.approve_by_source, db.approve_by_target, db.approve_by_admin, db.cancel_merge):
        with pytest.raises(ValueError):
            function(request["id"], other)
    db.approve_by_admin(request["id"], admin)
    with pytest.raises(ValueError):
        db.execute_merge(request["id"], other)
    assert db.get_merge_request(request["id"])["status"] == "approved"
    db.cancel_merge(request["id"], target)
    assert db.get_account_by_id(source)["pending_merge_target_id"] is None


def test_admin_tenant_privileges_cannot_be_inherited_by_regular_target(accounts):
    admin, source, _, _, _ = accounts
    with pytest.raises(ValueError, match="Demote"):
        db.admin_execute_merge(admin, source, admin)
    assert not db.get_account_by_id(admin)["suspended"]
