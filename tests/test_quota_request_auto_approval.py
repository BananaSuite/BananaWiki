from datetime import datetime, timedelta, timezone

import pytest

import db


QUOTA_TYPES = {
    "reservation": {
        "setting": "reservation_quota_auto_approve_max",
        "create": db.create_reservation_quota_request,
        "user_field": "reserved_pages_quota",
    },
    "contribution": {
        "setting": "contribution_quota_auto_approve_max",
        "create": db.create_contribution_quota_request,
        "user_field": "contribution_quota",
    },
}


def test_admin_settings_exposes_and_persists_separate_auto_approval_thresholds(
    logged_in_admin
):
    response = logged_in_admin.get("/global-settings")
    assert response.status_code == 200
    assert b'name="reservation_quota_auto_approve_max"' in response.data
    assert b'name="contribution_quota_auto_approve_max"' in response.data

    response = logged_in_admin.post(
        "/global-settings",
        data={
            "site_name": "BananaWiki",
            "timezone": "UTC",
            "reservation_quota_auto_approve_max": "8",
            "contribution_quota_auto_approve_max": "12",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    settings = db.get_site_settings()
    assert settings["reservation_quota_auto_approve_max"] == 8
    assert settings["contribution_quota_auto_approve_max"] == 12


def test_existing_quota_request_tables_gain_review_reason_migration(regular_user):
    with db.get_db_context() as conn:
        conn.execute("DROP TABLE reservation_quota_requests")
        conn.execute("DROP TABLE contribution_quota_requests")
        for table in ("reservation_quota_requests", "contribution_quota_requests"):
            conn.execute(
                f"""
                CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    requested_quota INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','denied','cancelled')),
                    reviewed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                f"INSERT INTO {table} (user_id, requested_quota, reason) VALUES (?, 7, ?)",
                (regular_user, f"Legacy {table}"),
            )
        conn.execute("PRAGMA user_version=0")  # Fixture represents a pre-versioned installation.
        conn.commit()

    db.init_db()

    with db.get_db_context() as conn:
        for table in ("reservation_quota_requests", "contribution_quota_requests"):
            columns = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            row = conn.execute(
                f"SELECT review_reason, review_source FROM {table}"
            ).fetchone()
            assert "review_reason" in columns
            assert "review_source" in columns
            assert row["review_reason"] == ""
            assert row["review_source"] == "manual"


@pytest.mark.parametrize("quota_type", QUOTA_TYPES)
def test_finite_quota_request_at_threshold_is_auto_approved(quota_type, regular_user):
    quota = QUOTA_TYPES[quota_type]
    db.update_site_settings(**{quota["setting"]: 10})

    request_row = quota["create"](regular_user, 10, "Need room for more work")

    assert request_row["status"] == "approved"
    assert request_row["reviewed_by"] is None
    assert request_row["reviewed_at"]
    assert request_row["review_source"] == "automatic"
    assert "threshold of 10" in request_row["review_reason"]
    assert db.get_user_by_id(regular_user)[quota["user_field"]] == 10


@pytest.mark.parametrize("quota_type", QUOTA_TYPES)
@pytest.mark.parametrize(
    ("threshold", "requested_quota"),
    [
        (0, 5),
        (10, 11),
        (10, -1),
    ],
    ids=["disabled", "above-threshold", "unlimited"],
)
def test_quota_request_is_not_auto_approved(
    quota_type, threshold, requested_quota, regular_user
):
    quota = QUOTA_TYPES[quota_type]
    db.update_site_settings(**{quota["setting"]: threshold})

    request_row = quota["create"](
        regular_user, requested_quota, "This request needs review"
    )

    assert request_row["status"] == "pending"
    assert request_row["reviewed_at"] is None
    assert request_row["review_reason"] == ""
    assert db.get_user_by_id(regular_user)[quota["user_field"]] is None


@pytest.mark.parametrize("quota_type", QUOTA_TYPES)
def test_lower_quota_request_is_not_auto_approved(quota_type, regular_user):
    quota = QUOTA_TYPES[quota_type]
    db.update_site_settings(**{quota["setting"]: 10})
    with db.get_db_context() as conn:
        conn.execute(
            f"UPDATE users SET {quota['user_field']}=9 WHERE id=?",
            (regular_user,),
        )
        conn.commit()

    request_row = quota["create"](
        regular_user, 8, "A lower quota needs an administrator's decision"
    )

    assert request_row["status"] == "pending"
    assert db.get_user_by_id(regular_user)[quota["user_field"]] == 9


@pytest.mark.parametrize("quota_type", QUOTA_TYPES)
def test_pending_request_blocks_a_second_auto_approved_request(quota_type, regular_user):
    quota = QUOTA_TYPES[quota_type]
    db.update_site_settings(**{quota["setting"]: 0})
    quota["create"](regular_user, 8, "First request needs review")
    db.update_site_settings(**{quota["setting"]: 10})

    with pytest.raises(ValueError, match="already have a pending"):
        quota["create"](regular_user, 9, "Must not bypass the pending request")

    assert db.get_user_by_id(regular_user)[quota["user_field"]] is None


@pytest.mark.parametrize("quota_type", QUOTA_TYPES)
def test_quota_request_cooldown_is_enforced_but_malformed_dates_are_tolerated(
    quota_type, regular_user
):
    quota = QUOTA_TYPES[quota_type]
    db.update_site_settings(quota_request_cooldown_hours=1)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with db.get_db_context() as conn:
        conn.execute(
            "UPDATE users SET quota_request_cooldown_until=? WHERE id=?",
            (future, regular_user),
        )
        conn.commit()

    with pytest.raises(ValueError, match="must wait"):
        quota["create"](regular_user, 8, "Cooldown should block this")

    with db.get_db_context() as conn:
        conn.execute(
            "UPDATE users SET quota_request_cooldown_until=? WHERE id=?",
            ("not-a-date", regular_user),
        )
        conn.commit()

    request_row = quota["create"](regular_user, 8, "Malformed dates are ignored")
    assert request_row["status"] == "pending"


def test_reservation_auto_approval_route_flashes_result(client, admin_user, editor_user):
    client.post("/login", data={"username": "editor", "password": "editor123"})
    db.update_site_settings(reservation_quota_auto_approve_max=10)

    response = client.post(
        "/settings/reservation-quota",
        data={
            "action": "submit_quota_request",
            "requested_quota": "7",
            "reason": "Several related pages need reservations",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Quota request automatically approved. Your quota has been updated." in response.data
    assert b"Automatic approval" in response.data


def test_contribution_auto_approval_route_flashes_result(
    client, admin_user, regular_user
):
    client.post("/login", data={"username": "user", "password": "user123"})
    db.update_site_settings(
        contribution_approval_enabled=1,
        contribution_quota_auto_approve_max=10,
    )

    response = client.post(
        "/my-contributions/quota-request",
        data={"requested_quota": "7", "reason": "Several drafts are in progress"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Quota request automatically approved. Your quota has been updated." in response.data
    assert db.get_user_by_id(regular_user)["contribution_quota"] == 7


def test_manual_reservation_review_reason_is_visible(
    logged_in_editor, editor_user, admin_user
):
    request_row = db.create_reservation_quota_request(
        editor_user, 8, "Original reservation reason"
    )
    logged_in_editor.get("/logout")
    logged_in_editor.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )

    response = logged_in_editor.post(
        f"/admin/users/{editor_user}/reservation-quota",
        data={
            "action": "deny_request",
            "request_id": request_row["id"],
            "review_reason": "Please finish the current reservations first.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Please finish the current reservations first." in response.data
    reviewed = db.get_reservation_quota_request(request_row["id"])
    assert reviewed["review_reason"] == "Please finish the current reservations first."
    assert reviewed["reviewed_by"] == admin_user
    assert reviewed["review_source"] == "manual"


def test_contribution_resolved_request_history_shows_full_review(
    logged_in_user, regular_user, admin_user
):
    db.update_site_settings(contribution_approval_enabled=1)
    request_row = db.create_contribution_quota_request(
        regular_user, 12, "Original contribution quota reason"
    )
    logged_in_user.get("/logout")
    logged_in_user.post(
        "/login", data={"username": "admin", "password": "admin123"}
    )
    logged_in_user.post(
        f"/admin/contribution-quota-requests/{request_row['id']}/review",
        data={
            "action": "deny",
            "review_reason": "Show consistent contribution quality first.",
        },
    )
    logged_in_user.get("/logout")
    logged_in_user.post(
        "/login", data={"username": "user", "password": "user123"}
    )

    response = logged_in_user.get("/my-contributions")

    assert response.status_code == 200
    assert b"Original contribution quota reason" in response.data
    assert b"Denied" in response.data
    assert b"Show consistent contribution quality first." in response.data
    assert b"by admin" in response.data
    assert b'min="-1"' in response.data
    assert db.get_pending_contribution_quota_request(regular_user) is None


@pytest.mark.parametrize(
    "review",
    [db.review_reservation_quota_request, db.review_contribution_quota_request],
)
def test_quota_review_reason_is_limited_to_1000_characters(
    review, regular_user, admin_user
):
    create = (
        db.create_reservation_quota_request
        if review is db.review_reservation_quota_request
        else db.create_contribution_quota_request
    )
    request_row = create(regular_user, 9, "Needs manual review")

    with pytest.raises(ValueError, match="1000"):
        review(request_row["id"], admin_user, True, "x" * 1001)
