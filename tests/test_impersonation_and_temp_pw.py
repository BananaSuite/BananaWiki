import pytest
from datetime import datetime, timezone, timedelta
from werkzeug.security import check_password_hash, generate_password_hash
import db


def _enable_test_logging(monkeypatch, tmp_path):
    import config

    _reset_test_logging()
    log_file = str(tmp_path / "logs" / "bananawiki.log")
    monkeypatch.setattr(config, "LOGGING_LEVEL", "verbose")
    monkeypatch.setattr(config, "LOG_FILE", log_file)
    return log_file


def _reset_test_logging():
    import wiki_logger

    if wiki_logger._logger is not None:
        for handler in list(wiki_logger._logger.handlers):
            handler.close()
            wiki_logger._logger.removeHandler(handler)
    wiki_logger._logger = None
    wiki_logger._log_level = None


def test_impersonation(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get("/")
    assert b"admin" in resp.data

    client.post(f"/admin/users/{regular_user}/impersonate")

    resp = client.get("/")
    assert b"Impersonation Mode:" in resp.data
    assert b"user" in resp.data

    client.post("/admin/stop-impersonating")

    resp = client.get("/")
    assert b"admin" in resp.data
    assert b"/admin/stop-impersonating" not in resp.data


def test_owner_impersonation(client, admin_user):
    owner = db.create_user("protected", generate_password_hash("prot123"), role="owner")

    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{owner}/impersonate", follow_redirects=True)
    assert b"Only superusers can impersonate owners." in resp.data

    db.update_user(admin_user, is_superuser=1)

    client.post(f"/admin/users/{owner}/impersonate")
    resp = client.get("/")
    assert b"Impersonation Mode:" in resp.data
    assert b"protected" in resp.data


def test_admin_cannot_impersonate_peer_admin_without_superuser(client, admin_user):
    peer_admin = db.create_user("peeradmin", generate_password_hash("peer123"), role="admin")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{peer_admin}/impersonate", follow_redirects=True)
    assert b"Only superusers can impersonate admin accounts." in resp.data
    assert b"/admin/stop-impersonating" not in resp.data


def test_superuser_can_impersonate_peer_admin(client, admin_user):
    peer_admin = db.create_user("peeradmin", generate_password_hash("peer123"), role="admin")
    db.update_user(admin_user, is_superuser=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{peer_admin}/impersonate", follow_redirects=True)
    assert b"Impersonation Mode:" in resp.data
    assert b"peeradmin" in resp.data
    assert b"Started by" in resp.data
    assert b"admin" in resp.data


def test_cannot_start_nested_impersonation(client, admin_user, regular_user):
    peer_admin = db.create_user("peeradmin", generate_password_hash("peer123"), role="admin")
    db.update_user(admin_user, is_superuser=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(f"/admin/users/{peer_admin}/impersonate")

    resp = client.post(f"/admin/users/{regular_user}/impersonate", follow_redirects=True)
    assert b"Stop impersonating before starting another impersonation session." in resp.data
    with client.session_transaction() as sess:
        assert sess["user_id"] == peer_admin
        assert sess["impersonator_id"] == admin_user


def test_self_impersonation_rejected(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{admin_user}/impersonate", follow_redirects=True)
    assert b"You cannot impersonate yourself." in resp.data


def test_cannot_impersonate_suspended_user(client, admin_user, regular_user):
    db.update_user(regular_user, suspended=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{regular_user}/impersonate", follow_redirects=True)
    assert b"You cannot impersonate a suspended user." in resp.data


def test_cannot_impersonate_scheduled_for_deletion_user(client, admin_user, regular_user):
    future = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    db.set_user_expiry(regular_user, future, set_by=admin_user)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(f"/admin/users/{regular_user}/impersonate", follow_redirects=True)
    assert b"You cannot impersonate a user that is scheduled for automatic deletion." in resp.data


def test_impersonate_nonexistent_user_returns_404(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post("/admin/users/DOESNOTEX/impersonate")
    assert resp.status_code == 404


def test_stop_impersonating_when_not_impersonating(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post("/admin/stop-impersonating", follow_redirects=True)
    assert b"You are not impersonating anyone." in resp.data


def test_stop_impersonating_after_original_admin_deleted(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    client.post(f"/admin/users/{regular_user}/impersonate")
    resp = client.get("/")
    assert b"Impersonation Mode:" in resp.data

    db.delete_user(admin_user)
    resp = client.post("/admin/stop-impersonating", follow_redirects=True)
    assert b"Original admin account not found." in resp.data


def test_stop_impersonating_forces_relogin_when_session_invalidated(client, admin_user, regular_user):
    db.update_site_settings(session_limit_enabled=1)
    client.post("/login", data={"username": "admin", "password": "admin123"})

    client.post(f"/admin/users/{regular_user}/impersonate")

    db.update_user(admin_user, session_token=None)
    resp = client.post("/admin/stop-impersonating", follow_redirects=True)
    assert b"Your admin session was invalidated while impersonating." in resp.data


def test_stop_impersonating_when_target_was_suspended(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(f"/admin/users/{regular_user}/impersonate")
    db.update_user(regular_user, suspended=1, suspended_until=None)

    resp = client.post("/admin/stop-impersonating", follow_redirects=True)
    assert b"You have stopped impersonating and returned to your admin account." in resp.data
    with client.session_transaction() as sess:
        assert sess["user_id"] == admin_user
        assert "impersonator_id" not in sess


def test_stop_impersonating_rejects_suspended_original_admin(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post(f"/admin/users/{regular_user}/impersonate")
    db.update_user(admin_user, suspended=1, suspended_until=None)

    resp = client.post("/admin/stop-impersonating", follow_redirects=True)
    assert b"Your original admin account is suspended." in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess
        assert "impersonator_id" not in sess


def test_impersonation_logs_written(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    client.post(f"/admin/users/{regular_user}/impersonate")
    conn = db.get_db()
    logs = conn.execute(
        "SELECT * FROM impersonation_logs WHERE admin_id=? AND target_user_id=?",
        (admin_user, regular_user)
    ).fetchall()
    assert len(logs) == 1
    assert logs[0]["ended_at"] is None

    client.post("/admin/stop-impersonating")
    logs = conn.execute(
        "SELECT * FROM impersonation_logs WHERE admin_id=? AND target_user_id=?",
        (admin_user, regular_user)
    ).fetchall()
    assert len(logs) == 1
    assert logs[0]["ended_at"] is not None


def test_impersonated_actions_log_real_admin_and_show_in_audit(
    client, admin_user, editor_user, monkeypatch, tmp_path
):
    log_file = _enable_test_logging(monkeypatch, tmp_path)
    try:
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post(f"/admin/users/{editor_user}/impersonate")

        resp = client.post(
            "/create-page",
            data={"title": "Impersonated Page", "content": "created by impersonation"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        import wiki_logger
        for handler in wiki_logger.get_logger().handlers:
            handler.flush()

        with open(log_file, encoding="utf-8") as f:
            contents = f.read()
        assert "create_page" in contents
        assert "user=editor" in contents
        assert "impersonated_by=admin" in contents
        assert "impersonated_user=editor" in contents

        client.post("/admin/stop-impersonating")
        resp = client.get(f"/admin/users/{admin_user}/audit")
        assert resp.status_code == 200
        assert b"create_page" in resp.data
        assert b"impersonated_by=admin" in resp.data
    finally:
        _reset_test_logging()


def test_admin_can_suspend_peer_admin(client, admin_user):
    peer_admin = db.create_user("suspendadmin", generate_password_hash("peer123"), role="admin")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        f"/admin/users/{peer_admin}/edit",
        data={"action": "suspend", "suspend_duration": "permanent"},
        follow_redirects=True,
    )
    assert b"User suspended" in resp.data
    assert db.get_user_by_id(peer_admin)["suspended"] == 1


def test_admin_can_change_peer_admin_password(client, admin_user):
    peer_admin = db.create_user("peerpw", generate_password_hash("peer123"), role="admin")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        f"/admin/users/{peer_admin}/edit",
        data={
            "action": "change_password",
            "password": "newpass123",
            "confirm_password": "newpass123",
        },
        follow_redirects=True,
    )
    assert b"Password updated" in resp.data
    assert check_password_hash(db.get_user_by_id(peer_admin)["password"], "newpass123")

    resp = client.post(
        f"/admin/users/{peer_admin}/edit",
        data={"action": "set_temp_password", "password": "temp_password123"},
        follow_redirects=True,
    )
    assert b"Temporary password set" in resp.data
    assert db.get_user_by_id(peer_admin)["original_password_backup"] is not None


def test_admin_can_demote_and_delete_peer_admin(client, admin_user):
    peer_admin = db.create_user("peerdemote", generate_password_hash("peer123"), role="admin")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        f"/admin/users/{peer_admin}/edit",
        data={"action": "change_role", "role": "user"},
        follow_redirects=True,
    )
    assert b"Role updated" in resp.data
    assert db.get_user_by_id(peer_admin)["role"] == "user"

    db.update_user(peer_admin, role="admin")

    resp = client.post(
        f"/admin/users/{peer_admin}/assign-role",
        data={"identity": "std:user"},
        follow_redirects=True,
    )
    assert b"now identifies as User" in resp.data
    assert db.get_user_by_id(peer_admin)["role"] == "user"

    db.update_user(peer_admin, role="admin")

    resp = client.post(
        f"/admin/users/{peer_admin}/edit",
        data={"action": "delete"},
        follow_redirects=True,
    )
    assert b"User deleted" in resp.data
    assert db.get_user_by_id(peer_admin) is None


def test_admin_can_grant_admin_role(client, admin_user, regular_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        "/admin/users/create",
        data={
            "username": "newadmin",
            "password": "newadmin123",
            "confirm_password": "newadmin123",
            "role": "admin",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_by_username("newadmin")["role"] == "admin"

    resp = client.post(
        f"/admin/users/{regular_user}/edit",
        data={"action": "change_role", "role": "admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_by_id(regular_user)["role"] == "admin"

    db.update_user(regular_user, role="user")

    resp = client.post(
        f"/admin/users/{regular_user}/assign-role",
        data={"identity": "std:admin"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_by_id(regular_user)["role"] == "admin"


def test_admin_can_generate_admin_invite(client, admin_user):
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        "/admin/codes/generate",
        data={"max_uses": "1", "expiry_mode": "48h", "assigned_role": "admin"},
        follow_redirects=True,
    )
    assert b"Invite code generated" in resp.data
    assert [c for c in db.list_invite_codes(active_only=True) if c["assigned_role"] == "admin"]


def test_admin_can_manage_peer_admin_profile_tags_and_history(client, admin_user):
    peer_admin = db.create_user("peerprofile", generate_password_hash("peer123"), role="admin")
    tag_id = db.add_user_custom_tag(peer_admin, "Trusted", "#112233")
    db.record_role_change(peer_admin, "editor", "admin", changed_by=admin_user)
    role_history_id = db.get_role_history(peer_admin)[0]["id"]
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        f"/admin/users/{peer_admin}/profile",
        data={"action": "disable_profile"},
        follow_redirects=True,
    )
    assert b"User profile has been successfully disabled" in resp.data
    profile = db.get_user_profile(peer_admin)
    assert profile["page_disabled_by_admin"] == 1

    resp = client.post(
        f"/admin/users/{peer_admin}/tags",
        data={"action": "delete_tag", "tag_id": tag_id},
        follow_redirects=True,
    )
    assert b"Tag has been successfully deleted" in resp.data
    assert db.get_user_custom_tag(tag_id) is None

    resp = client.post(
        f"/admin/users/{peer_admin}/attributions",
        data={"action": "delete_role_history_entry", "entry_id": role_history_id},
        follow_redirects=True,
    )
    assert b"Role history entry deleted" in resp.data
    assert db.get_role_history_entry(role_history_id) is None


def test_admin_can_export_chat_toggle_and_quota_peer_admin(client, admin_user):
    peer_admin = db.create_user("peerdata", generate_password_hash("peer123"), role="admin")
    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.get(f"/admin/users/{peer_admin}/export")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"

    resp = client.post(f"/admin/users/{peer_admin}/toggle_chat", follow_redirects=True)
    assert b"Chat access disabled" in resp.data
    assert db.is_user_chat_disabled(peer_admin)

    resp = client.post(
        f"/admin/users/{peer_admin}/reservation-quota",
        data={"action": "set_quota", "new_quota": "99"},
        follow_redirects=True,
    )
    assert b"Reservation quota" in resp.data
    assert db.get_user_by_id(peer_admin)["reserved_pages_quota"] == 99


def test_admin_contribution_quota_actions_are_audited(client, admin_user, regular_user, monkeypatch, tmp_path):
    log_file = _enable_test_logging(monkeypatch, tmp_path)
    page_id = db.create_page("Audit Quota", "audit-quota", "Original")
    contribution_id = db.create_contribution(
        page_id,
        regular_user,
        "Audit Quota",
        "Updated",
        "Needs review",
    )
    db.create_contribution_quota_request(regular_user, 12, "More pending edits")
    request_id = db.get_pending_contribution_quota_request(regular_user)["id"]

    client.post("/login", data={"username": "admin", "password": "admin123"})

    resp = client.post(
        f"/admin/contributions/{contribution_id}/set-quota",
        data={"quota": "9"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_by_id(regular_user)["contribution_quota"] == 9

    resp = client.post(
        f"/admin/contribution-quota-requests/{request_id}/review",
        data={"action": "approve"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_user_by_id(regular_user)["contribution_quota"] == 12

    import wiki_logger
    for handler in wiki_logger.get_logger().handlers:
        handler.flush()
    with open(log_file, encoding="utf-8") as fh:
        contents = fh.read()
    assert "admin_set_contribution_quota" in contents
    assert "review_contribution_quota_request" in contents
    assert "user=admin" in contents
    assert "target_user=user" in contents


def test_editor_cannot_impersonate(client, admin_user, editor_user, regular_user):
    client.post("/login", data={"username": "editor", "password": "editor123"})
    resp = client.post(f"/admin/users/{regular_user}/impersonate", follow_redirects=True)
    assert b"Admin access required." in resp.data


def test_regular_user_cannot_impersonate(client, admin_user, regular_user, editor_user):
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.post(f"/admin/users/{editor_user}/impersonate", follow_redirects=True)
    assert b"Admin access required." in resp.data


def test_temporary_password(client, logged_in_admin, regular_user):
    # Set temp password
    client.post(f"/admin/users/{regular_user}/edit", data={
        "action": "set_temp_password",
        "password": "temp_password123"
    })

    user = db.get_user_by_id(regular_user)
    assert user["original_password_backup"] is not None
    assert check_password_hash(user["password"], "temp_password123")

    # Revert password
    client.post(f"/admin/users/{regular_user}/edit", data={
        "action": "revert_password"
    })

    user = db.get_user_by_id(regular_user)
    assert user["original_password_backup"] is None
    assert check_password_hash(user["password"], "user123")
