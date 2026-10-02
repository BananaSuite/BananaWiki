"""Account protection must survive overlapping sensitive requests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from conftest import PASSWORD

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.users import service


def test_concurrent_self_deletions_leave_one_active_admin(app, make_user, db, monkeypatch):
    users = [make_user("first", role="admin"), make_user("second", role="admin")]
    ready = Barrier(2)
    delete = service.delete_account

    def simultaneous_delete(*args, **kwargs):
        ready.wait(timeout=10)
        return delete(*args, **kwargs)

    monkeypatch.setattr(service, "delete_account", simultaneous_delete)

    def attempt(user):
        with app.test_request_context(), connection_scope():
            try:
                service.delete_own_account(user, PASSWORD)
            except service.ProfileError as error:
                return error.key
            return "deleted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(attempt, user) for user in users]
        assert sorted(request.result(timeout=15) for request in pending) == ["deleted", "users.error.last_admin"]
    assert db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND suspended = 0") == 1


def test_sensitive_password_budget_is_reserved_before_hashing(app, make_user, monkeypatch):
    user = make_user("member")
    hashing, finish_hash = Event(), Event()
    monkeypatch.setattr(service, "PASSWORD_CHECKS", 1)

    def slow_hash(*_args):
        hashing.set()
        assert finish_hash.wait(timeout=10)
        return False

    monkeypatch.setattr(service.passwords, "verify_password", slow_hash)

    def attempt():
        with app.test_request_context(), connection_scope():
            return service.password_ok(user, "incorrect")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt)
        assert hashing.wait(timeout=5)
        second = pool.submit(attempt)
        try:
            with pytest.raises(service.ProfileError, match="auth.error.too_many_attempts"):
                second.result(timeout=5)
        finally:
            finish_hash.set()
        assert first.result(timeout=5) is False


@pytest.mark.parametrize("change", ["demotion", "password_reset"])
def test_owner_toggle_rechecks_privileges_after_password_verification(app, make_user, db, monkeypatch, change):
    user = make_user("administrator", role="admin")

    def changed_during_verification(*_args):
        if change == "demotion":
            db.execute("UPDATE users SET role = 'user' WHERE id = ?", (user["id"],))
        else:
            db.execute("UPDATE users SET password = 'new-password-hash' WHERE id = ?", (user["id"],))
        return True

    monkeypatch.setattr(service, "password_ok", changed_during_verification)
    with app.test_request_context(), connection_scope(), pytest.raises(service.ProfileError):
        service.set_owner_status(user, PASSWORD)
    assert db.scalar("SELECT role FROM users WHERE id = ?", (user["id"],)) != "owner"
    assert db.scalar("SELECT COUNT(*) FROM role_history") == 0
