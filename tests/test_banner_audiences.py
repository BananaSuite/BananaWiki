"""Focused database tests for advanced BananaWiki announcement audiences."""

import db


def _user(username):
    return db.create_user(username, "test-password-hash")


def test_announcement_allowlist_and_denylist_filter_by_user():
    admin_id = db.create_user("banner-admin", "hash", role="admin")
    selected_id = _user("selected-user")
    other_id = _user("other-user")

    db.create_announcement(
        "Allowlisted", "blue", "normal", "both", None, admin_id,
        audience_mode="allowlist", audience_user_ids=[selected_id],
    )
    db.create_announcement(
        "Denied", "red", "normal", "both", None, admin_id,
        audience_mode="denylist", audience_user_ids=[selected_id],
    )

    selected = {row["content"] for row in db.get_active_announcements(True, selected_id)}
    other = {row["content"] for row in db.get_active_announcements(True, other_id)}
    guest = {row["content"] for row in db.get_active_announcements(False)}

    assert "Allowlisted" in selected
    assert "Denied" not in selected
    assert "Allowlisted" not in other
    assert "Denied" in other
    assert "Allowlisted" not in guest
    assert "Denied" in guest


def test_announcement_update_replaces_targets_and_increments_revision():
    admin_id = db.create_user("banner-owner", "hash", role="admin")
    first_id = _user("first-target")
    second_id = _user("second-target")
    announcement_id = db.create_announcement(
        "Targeted", "orange", "normal", "logged_in", None, admin_id,
        audience_mode="allowlist", audience_user_ids=[first_id],
        custom_background="#123456", custom_text_color="#ffffff",
    )

    db.update_announcement(
        announcement_id,
        audience_user_ids=[second_id],
        content="Retargeted",
    )

    announcement = db.get_announcement(announcement_id)
    assert announcement["revision"] == 2
    assert announcement["custom_background"] == "#123456"
    assert db.get_announcement_audience_user_ids(announcement_id) == [second_id]
    assert db.get_visible_announcement(announcement_id, True, first_id) is None
    assert db.get_visible_announcement(announcement_id, True, second_id) is not None


def test_announcement_audience_is_included_in_site_export():
    admin_id = db.create_user("export-banner-owner", "hash", role="admin")
    target_id = _user("export-banner-target")
    announcement_id = db.create_announcement(
        "Exported targeting", "green", "normal", "both", None, admin_id,
        audience_mode="allowlist", audience_user_ids=[target_id],
    )

    exported = db.export_site_data()
    rows = exported["announcement_audience_users"]
    assert any(
        row["announcement_id"] == announcement_id and row["user_id"] == target_id
        for row in rows
    )
