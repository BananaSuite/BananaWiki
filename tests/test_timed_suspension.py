"""Tests for the timed user suspension feature."""

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash


def test_is_suspension_active_not_suspended(isolated_db):
    """is_suspension_active returns False for non-suspended users."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    user = db.get_user_by_id(uid)
    assert db.is_suspension_active(user) is False


def test_is_suspension_active_permanent(isolated_db):
    """is_suspension_active returns True for permanently suspended users."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspended_until=None)
    user = db.get_user_by_id(uid)
    assert db.is_suspension_active(user) is True


def test_is_suspension_active_timed_future(isolated_db):
    """is_suspension_active returns True when suspended_until is in the future."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    user = db.get_user_by_id(uid)
    assert db.is_suspension_active(user) is True


def test_is_suspension_active_timed_expired(isolated_db):
    """is_suspension_active returns False when suspended_until is in the past."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past)
    user = db.get_user_by_id(uid)
    assert db.is_suspension_active(user) is False


def test_check_suspension_expired_auto_unsuspends(isolated_db):
    """check_suspension_expired unsuspends when timed suspension has elapsed."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past)
    result = db.check_suspension_expired(uid)
    assert result is True
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0
    assert user["suspended_until"] is None


def test_check_suspension_expired_permanent_not_unsuspended(isolated_db):
    """check_suspension_expired does not unsuspend permanent suspensions."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspended_until=None)
    result = db.check_suspension_expired(uid)
    assert result is False
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1


def test_check_suspension_expired_future_not_unsuspended(isolated_db):
    """check_suspension_expired does not unsuspend active timed suspensions."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    result = db.check_suspension_expired(uid)
    assert result is False
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1


def test_check_suspension_expired_non_suspended_user(isolated_db):
    """check_suspension_expired returns False for non-suspended users."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    result = db.check_suspension_expired(uid)
    assert result is False


def test_admin_suspend_permanent(logged_in_admin, admin_user):
    """Admin can suspend a user permanently (default)."""
    import db
    uid = db.create_user("victim", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "suspend", "suspend_duration": "permanent"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"suspended permanently" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspended_until"] is None


def test_admin_suspend_timed(logged_in_admin, admin_user):
    """Admin can suspend a user for a specific duration."""
    import db
    uid = db.create_user("victim2", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "suspend", "suspend_duration": "24"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User suspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspended_until"] is not None
    # Verify the expiry is roughly 24 hours from now
    expiry = datetime.fromisoformat(user["suspended_until"]).replace(tzinfo=timezone.utc)
    diff = expiry - datetime.now(timezone.utc)
    assert 23 * 3600 < diff.total_seconds() < 25 * 3600


def test_admin_unsuspend_clears_suspended_until(logged_in_admin, admin_user):
    """Unsuspending a user clears the suspended_until field."""
    import db
    uid = db.create_user("victim3", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "unsuspend"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User unsuspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0
    assert user["suspended_until"] is None


def test_login_blocked_for_timed_suspension(client, admin_user):
    """A user with an active timed suspension cannot login."""
    import db
    uid = db.create_user("tsuser", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    resp = client.post("/login", data={"username": "tsuser", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"issue with your account" in resp.data.lower()


def test_login_allowed_after_timed_suspension_expires(client, admin_user):
    """A user whose timed suspension has expired can login."""
    import db
    uid = db.create_user("tsuser2", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past)
    resp = client.post("/login", data={"username": "tsuser2", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 200
    # Should be logged in now, not seeing a suspension message
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_login_blocked_for_permanent_suspension(client, admin_user):
    """A permanently suspended user cannot login."""
    import db
    uid = db.create_user("permuser", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspended_until=None)
    resp = client.post("/login", data={"username": "permuser", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"issue with your account" in resp.data.lower()


def test_session_auto_unsuspend_expired(client, admin_user):
    """An active session auto-unsuspends when a timed suspension has expired."""
    import db
    uid = db.create_user("sesuser", generate_password_hash("pw"), role="user")
    # Log in first
    client.post("/login", data={"username": "sesuser", "password": "pw"})
    # Now suspend with an already-expired time
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past)
    # Access a protected page: should auto-unsuspend
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_session_kicks_active_timed_suspension(client, admin_user):
    """An active session is terminated for users with an active timed suspension."""
    import db
    uid = db.create_user("sesuser2", generate_password_hash("pw"), role="user")
    # Log in first
    client.post("/login", data={"username": "sesuser2", "password": "pw"})
    # Now suspend with a future time
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    # Access a protected page: should be redirected to account-suspended
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 403
    assert b"issue with your account" in resp.data.lower()


def test_admin_users_shows_suspension_type(logged_in_admin, admin_user):
    """The admin users list shows suspension duration info."""
    import db
    uid_perm = db.create_user("permsus", generate_password_hash("pw"), role="user")
    db.update_user(uid_perm, suspended=1, suspended_until=None)
    uid_timed = db.create_user("timedsus", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid_timed, suspended=1, suspended_until=future)
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    assert b"permanent" in resp.data
    # Timed suspension shows "expires" text
    assert b"expires" in resp.data


def test_cleanup_expired_suspensions_unsuspends(isolated_db):
    """cleanup_expired_suspensions auto-unsuspends users with elapsed timed suspensions."""
    import db
    uid = db.create_user("cleanup1", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past)
    count = db.cleanup_expired_suspensions()
    assert count == 1
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0
    assert user["suspended_until"] is None


def test_cleanup_expired_suspensions_skips_permanent(isolated_db):
    """cleanup_expired_suspensions does not touch permanent suspensions."""
    import db
    uid = db.create_user("cleanup2", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspended_until=None)
    count = db.cleanup_expired_suspensions()
    assert count == 0
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1


def test_cleanup_expired_suspensions_skips_active(isolated_db):
    """cleanup_expired_suspensions does not unsuspend active timed suspensions."""
    import db
    uid = db.create_user("cleanup3", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future)
    count = db.cleanup_expired_suspensions()
    assert count == 0
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1


def test_run_full_cleanup_includes_suspensions(isolated_db):
    """run_full_cleanup includes expired_suspensions in the summary."""
    import db
    result = db.run_full_cleanup(vacuum=False)
    assert "expired_suspensions" in result


def test_cleanup_unsuspends_multiple_users(isolated_db):
    """cleanup_expired_suspensions handles multiple expired suspensions at once."""
    import db
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    uid1 = db.create_user("multi1", generate_password_hash("pw"), role="user")
    uid2 = db.create_user("multi2", generate_password_hash("pw"), role="editor")
    db.update_user(uid1, suspended=1, suspended_until=past)
    db.update_user(uid2, suspended=1, suspended_until=past)
    count = db.cleanup_expired_suspensions()
    assert count == 2
    assert db.get_user_by_id(uid1)["suspended"] == 0
    assert db.get_user_by_id(uid2)["suspended"] == 0
