"""Tests for the advanced user suspension & audit system."""

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash


def test_record_and_get_suspension_history(isolated_db):
    """record_suspension_action creates entries retrievable via get_suspension_history."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    admin_uid = db.create_user("admin1", generate_password_hash("pw"), role="admin")
    db.record_suspension_action(
        uid, "suspend",
        performed_by=admin_uid,
        reason="Breaking rules",
        reason_visible=True,
        time_visible=False,
        duration="permanent",
        suspended_until=None,
    )
    history = db.get_suspension_history(uid)
    assert len(history) == 1
    entry = history[0]
    assert entry["action"] == "suspend"
    assert entry["reason"] == "Breaking rules"
    assert entry["reason_visible"] == 1
    assert entry["time_visible"] == 0
    assert entry["duration"] == "permanent"
    assert entry["suspended_until"] is None
    assert entry["performed_by_username"] == "admin1"


def test_suspension_history_ordered_newest_first(isolated_db):
    """get_suspension_history returns entries in reverse chronological order."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    db.record_suspension_action(uid, "suspend", reason="first")
    db.record_suspension_action(uid, "unsuspend")
    db.record_suspension_action(uid, "suspend", reason="second")
    history = db.get_suspension_history(uid)
    assert len(history) == 3
    assert history[0]["reason"] == "second"
    assert history[1]["action"] == "unsuspend"
    assert history[2]["reason"] == "first"


def test_suspension_history_empty_for_new_user(isolated_db):
    """get_suspension_history returns empty list for a user with no suspension history."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    assert db.get_suspension_history(uid) == []


def test_user_suspend_reason_columns(isolated_db):
    """New suspend_reason, suspend_reason_visible, and suspend_time_visible columns work."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    db.update_user(
        uid,
        suspended=1,
        suspend_reason="Spamming",
        suspend_reason_visible=1,
        suspend_time_visible=0,
    )
    user = db.get_user_by_id(uid)
    assert user["suspend_reason"] == "Spamming"
    assert user["suspend_reason_visible"] == 1
    assert user["suspend_time_visible"] == 0


def test_unsuspend_clears_suspension_metadata(isolated_db):
    """Unsuspending a user via update_user clears all suspension metadata."""
    import db
    uid = db.create_user("u1", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(
        uid,
        suspended=1,
        suspended_until=future,
        suspend_reason="Test",
        suspend_reason_visible=1,
        suspend_time_visible=1,
    )
    db.update_user(
        uid,
        suspended=0,
        suspended_until=None,
        suspend_reason=None,
        suspend_reason_visible=0,
        suspend_time_visible=0,
    )
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0
    assert user["suspend_reason"] is None
    assert user["suspend_reason_visible"] == 0
    assert user["suspend_time_visible"] == 0


def test_admin_suspend_with_reason(logged_in_admin, admin_user):
    """Admin can suspend a user with a reason."""
    import db
    uid = db.create_user("victim", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "permanent",
            "suspend_reason": "Breaking rules",
            "suspend_reason_visible": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"suspended permanently" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspend_reason"] == "Breaking rules"
    assert user["suspend_reason_visible"] == 1


def test_admin_suspend_timed_with_visibility(logged_in_admin, admin_user):
    """Admin can suspend a user for a timed duration with time visible to user."""
    import db
    uid = db.create_user("victim2", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "24",
            "suspend_reason": "Temporary ban",
            "suspend_reason_visible": "1",
            "suspend_time_visible": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User suspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspended_until"] is not None
    assert user["suspend_reason"] == "Temporary ban"
    assert user["suspend_reason_visible"] == 1
    assert user["suspend_time_visible"] == 1


def test_admin_suspend_permanent_hides_time_visible(logged_in_admin, admin_user):
    """Permanent suspension ignores time_visible since there's no end time."""
    import db
    uid = db.create_user("victim3", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "permanent",
            "suspend_time_visible": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspend_time_visible"] == 0


def test_admin_unsuspend_clears_metadata(logged_in_admin, admin_user):
    """Unsuspending a user clears all suspension metadata and records audit."""
    import db
    uid = db.create_user("victim4", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspend_reason="Test", suspend_reason_visible=1)
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "unsuspend"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User unsuspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0
    assert user["suspend_reason"] is None
    assert user["suspend_reason_visible"] == 0
    history = db.get_suspension_history(uid)
    assert any(e["action"] == "unsuspend" for e in history)


def test_admin_suspend_records_audit(logged_in_admin, admin_user):
    """Suspending a user creates a suspension_audit entry."""
    import db
    uid = db.create_user("victim5", generate_password_hash("pw"), role="user")
    logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "1",
            "suspend_reason": "Testing audit",
        },
        follow_redirects=True,
    )
    history = db.get_suspension_history(uid)
    assert len(history) >= 1
    entry = history[0]
    assert entry["action"] == "suspend"
    assert entry["reason"] == "Testing audit"
    assert entry["performed_by"] == admin_user


def test_admin_suspend_custom_relative(logged_in_admin, admin_user):
    """Admin can suspend with a custom relative time (hours + minutes)."""
    import db
    uid = db.create_user("victim6", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_relative",
            "suspend_rel_hours": "2",
            "suspend_rel_minutes": "30",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"User suspended" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    assert user["suspended_until"] is not None
    expiry = datetime.fromisoformat(user["suspended_until"]).replace(tzinfo=timezone.utc)
    diff = expiry - datetime.now(timezone.utc)
    # Should be roughly 2.5 hours from now
    assert 2 * 3600 < diff.total_seconds() < 3 * 3600


def test_admin_suspend_8_hours(logged_in_admin, admin_user):
    """Admin can suspend for 8 hours (new option)."""
    import db
    uid = db.create_user("victim7", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "suspend", "suspend_duration": "8"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    expiry = datetime.fromisoformat(user["suspended_until"]).replace(tzinfo=timezone.utc)
    diff = expiry - datetime.now(timezone.utc)
    assert 7 * 3600 < diff.total_seconds() < 9 * 3600


def test_admin_suspend_1_week(logged_in_admin, admin_user):
    """Admin can suspend for 1 week (168 hours)."""
    import db
    uid = db.create_user("victim8", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={"action": "suspend", "suspend_duration": "168"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 1
    expiry = datetime.fromisoformat(user["suspended_until"]).replace(tzinfo=timezone.utc)
    diff = expiry - datetime.now(timezone.utc)
    assert 167 * 3600 < diff.total_seconds() < 169 * 3600


def test_login_shows_reason_when_visible(client, admin_user):
    """Suspended user sees the reason on the suspension page when reason_visible is set."""
    import db
    uid = db.create_user("banned1", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspend_reason="Spamming", suspend_reason_visible=1)
    resp = client.post("/login", data={"username": "banned1", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"Spamming" in resp.data


def test_login_hides_reason_when_not_visible(client, admin_user):
    """Suspended user does NOT see the reason when reason_visible is not set."""
    import db
    uid = db.create_user("banned2", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspend_reason="Secret reason", suspend_reason_visible=0)
    resp = client.post("/login", data={"username": "banned2", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"Secret reason" not in resp.data
    assert b"issue with your account" in resp.data.lower()


def test_login_shows_permanent_message(client, admin_user):
    """Permanently suspended user sees 'permanent' in the suspension page."""
    import db
    uid = db.create_user("banned3", generate_password_hash("pw"), role="user")
    db.update_user(uid, suspended=1, suspended_until=None)
    resp = client.post("/login", data={"username": "banned3", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"permanent" in resp.data.lower()


def test_login_shows_time_when_visible(client, admin_user):
    """Suspended user sees expiry time on the suspension page when time_visible is set."""
    import db
    uid = db.create_user("banned4", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future, suspend_time_visible=1)
    resp = client.post("/login", data={"username": "banned4", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    # The expiry section is shown with the "Access will be restored" label
    assert b"Access will be restored" in resp.data


def test_login_hides_time_when_not_visible(client, admin_user):
    """Suspended user does NOT see expiry when time_visible is not set."""
    import db
    uid = db.create_user("banned5", generate_password_hash("pw"), role="user")
    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=future, suspend_time_visible=0)
    resp = client.post("/login", data={"username": "banned5", "password": "pw"},
                       follow_redirects=True)
    assert resp.status_code == 403
    assert b"Access will be restored" not in resp.data
    assert b"issue with your account" in resp.data.lower()


def test_auto_unsuspend_clears_metadata(isolated_db):
    """check_suspension_expired clears suspension metadata on auto-unsuspend."""
    import db
    uid = db.create_user("auto1", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past,
                   suspend_reason="Test", suspend_reason_visible=1, suspend_time_visible=1)
    result = db.check_suspension_expired(uid)
    assert result is True
    user = db.get_user_by_id(uid)
    assert user["suspend_reason"] is None
    assert user["suspend_reason_visible"] == 0
    assert user["suspend_time_visible"] == 0


def test_cleanup_expired_suspensions_clears_metadata(isolated_db):
    """cleanup_expired_suspensions clears suspension metadata."""
    import db
    uid = db.create_user("cleanup1", generate_password_hash("pw"), role="user")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db.update_user(uid, suspended=1, suspended_until=past,
                   suspend_reason="Old ban", suspend_reason_visible=1, suspend_time_visible=1)
    count = db.cleanup_expired_suspensions()
    assert count == 1
    user = db.get_user_by_id(uid)
    assert user["suspend_reason"] is None
    assert user["suspend_reason_visible"] == 0
    assert user["suspend_time_visible"] == 0


def test_audit_page_shows_suspension_history(logged_in_admin, admin_user):
    """The audit page shows a suspension history section when history exists."""
    import db
    uid = db.create_user("audited", generate_password_hash("pw"), role="user")
    db.record_suspension_action(
        uid, "suspend",
        performed_by=admin_user,
        reason="Test ban",
        reason_visible=True,
        duration="permanent",
    )
    resp = logged_in_admin.get(f"/admin/users/{uid}/audit")
    assert resp.status_code == 200
    assert b"Suspension History" in resp.data
    assert b"Test ban" in resp.data


def test_audit_page_no_suspension_history(logged_in_admin, admin_user):
    """The audit page does not show suspension section when history is empty."""
    import db
    uid = db.create_user("clean", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.get(f"/admin/users/{uid}/audit")
    assert resp.status_code == 200
    assert b'id="suspension-history-section"' not in resp.data


def test_admin_users_page_has_suspend_modal(logged_in_admin, admin_user):
    """The admin users page includes the suspend modal HTML."""
    import db
    db.create_user("testuser", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    assert b"suspend-modal" in resp.data
    assert b"openSuspendModal" in resp.data
    assert b"suspend_reason" in resp.data


def test_reason_visible_requires_reason(logged_in_admin, admin_user):
    """reason_visible is not set when no reason is provided."""
    import db
    uid = db.create_user("noreason", generate_password_hash("pw"), role="user")
    logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "permanent",
            "suspend_reason": "",
            "suspend_reason_visible": "1",
        },
        follow_redirects=True,
    )
    user = db.get_user_by_id(uid)
    assert user["suspend_reason_visible"] == 0


def test_time_visible_not_set_for_permanent(logged_in_admin, admin_user):
    """time_visible is forced to 0 for permanent suspensions."""
    import db
    uid = db.create_user("permtest", generate_password_hash("pw"), role="user")
    logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "permanent",
            "suspend_time_visible": "1",
        },
        follow_redirects=True,
    )
    user = db.get_user_by_id(uid)
    assert user["suspend_time_visible"] == 0


def test_suspend_reason_truncated_to_500(logged_in_admin, admin_user):
    """Suspension reason is truncated to 500 characters."""
    import db
    uid = db.create_user("longr", generate_password_hash("pw"), role="user")
    long_reason = "x" * 600
    logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "permanent",
            "suspend_reason": long_reason,
        },
        follow_redirects=True,
    )
    user = db.get_user_by_id(uid)
    assert len(user["suspend_reason"]) == 500


def test_suspend_custom_datetime_invalid_format(logged_in_admin, admin_user):
    """Invalid custom datetime format flashes error and does NOT suspend."""
    import db
    uid = db.create_user("victim_dt1", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_datetime",
            "suspend_custom_datetime": "not-a-date",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid suspension date/time" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspend_custom_datetime_empty(logged_in_admin, admin_user):
    """Empty custom datetime flashes error and does NOT suspend."""
    import db
    uid = db.create_user("victim_dt2", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_datetime",
            "suspend_custom_datetime": "",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"required for custom date suspension" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspend_custom_datetime_in_past(logged_in_admin, admin_user):
    """Custom datetime in the past flashes error and does NOT suspend."""
    import db
    past_dt = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    uid = db.create_user("victim_dt3", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_datetime",
            "suspend_custom_datetime": past_dt,
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Suspension date must be in the future" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspend_custom_relative_invalid_values(logged_in_admin, admin_user):
    """Non-numeric custom relative values flash error and do NOT suspend."""
    import db
    uid = db.create_user("victim_rel1", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_relative",
            "suspend_rel_hours": "abc",
            "suspend_rel_minutes": "xyz",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid suspension duration" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspend_custom_relative_zero_duration(logged_in_admin, admin_user):
    """Zero hours and zero minutes flashes error and does NOT suspend."""
    import db
    uid = db.create_user("victim_rel2", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.post(
        f"/admin/users/{uid}/edit",
        data={
            "action": "suspend",
            "suspend_duration": "custom_relative",
            "suspend_rel_hours": "0",
            "suspend_rel_minutes": "0",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Suspension duration must be greater than zero" in resp.data
    user = db.get_user_by_id(uid)
    assert user["suspended"] == 0


def test_suspend_modal_username_uses_tojson_escaping(logged_in_admin, admin_user):
    """Suspend button passes user data safely via data attributes."""
    import db

    uid = db.create_user("test_user", generate_password_hash("pw"), role="user")
    resp = logged_in_admin.get("/admin/users")
    assert resp.status_code == 200
    html = resp.data.decode()
    # data attributes carry user id and username safely
    assert f'data-user-id="{uid}"' in html
    assert 'data-username="test_user"' in html
