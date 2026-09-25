"""Tests for the database cleanup and optimization routines.

Covers db/_cleanup.py, the improved get_all_referenced_image_filenames(),
cleanup_orphaned_attachments(), and associated database cleanup.
"""

import os
import json
import sqlite3
from datetime import datetime, timezone

import pytest
import config


def _backdate(table, column, row_id, days, id_column="id"):
    """Shift a datetime column back by *days* using raw SQL."""
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        f"UPDATE {table} SET {column} = datetime('now', '-{days} days') WHERE {id_column} = ?",
        (row_id,),
    )
    conn.commit()
    conn.close()


def _backdate_where(table, column, where_clause, params, days):
    """Shift a datetime column back by *days* for rows matching the WHERE."""
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        f"UPDATE {table} SET {column} = datetime('now', '-{days} days') WHERE {where_clause}",
        params,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
#  cleanup_expired_invite_codes
# ---------------------------------------------------------------------------

class TestCleanupExpiredInviteCodes:
    def test_removes_old_expired_codes(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("inv_admin", generate_password_hash("pw"), role="admin")
        code = db.generate_invite_code(uid)
        # Backdate the invite so it's old enough to be cleaned
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE invite_codes SET created_at = datetime('now', '-60 days'), "
            "expires_at = datetime('now', '-59 days') WHERE code = ?",
            (code,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_expired_invite_codes(retention_days=30)
        assert removed == 1

    def test_keeps_recent_active_codes(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("inv_admin2", generate_password_hash("pw"), role="admin")
        db.generate_invite_code(uid)

        removed = db.cleanup_expired_invite_codes(retention_days=30)
        assert removed == 0
        # Code should still exist
        codes = db.list_invite_codes(active_only=True)
        assert len(codes) == 1

    def test_removes_old_used_codes(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("inv_admin3", generate_password_hash("pw"), role="admin")
        code = db.generate_invite_code(uid)
        uid2 = db.create_user("inv_user", generate_password_hash("pw"), role="user")
        db.use_invite_code(code, uid2)
        # Backdate so it's old
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE invite_codes SET created_at = datetime('now', '-60 days') WHERE code = ?",
            (code,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_expired_invite_codes(retention_days=30)
        assert removed == 1

    def test_removes_old_soft_deleted_codes(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("inv_admin4", generate_password_hash("pw"), role="admin")
        code = db.generate_invite_code(uid)
        # soft delete and backdate
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE invite_codes SET deleted = 1, "
            "created_at = datetime('now', '-60 days') WHERE code = ?",
            (code,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_expired_invite_codes(retention_days=30)
        assert removed == 1


# ---------------------------------------------------------------------------
#  cleanup_stale_drafts
# ---------------------------------------------------------------------------

class TestCleanupStaleDrafts:
    def test_removes_old_drafts(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("duser", generate_password_hash("pw"), role="editor")
        pid = db.create_page("DraftPage", "draft-page", "content", None, uid)
        db.save_draft(pid, uid, "Stale draft", "stale content")
        # Backdate the draft
        _backdate_where("drafts", "updated_at", "page_id = ? AND user_id = ?",
                        (pid, uid), 60)

        removed = db.cleanup_stale_drafts(retention_days=30)
        assert removed == 1
        assert db.get_draft(pid, uid) is None

    def test_keeps_recent_drafts(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("duser2", generate_password_hash("pw"), role="editor")
        pid = db.create_page("DraftPage2", "draft-page-2", "content", None, uid)
        db.save_draft(pid, uid, "Recent draft", "recent content")

        removed = db.cleanup_stale_drafts(retention_days=30)
        assert removed == 0
        assert db.get_draft(pid, uid) is not None


# ---------------------------------------------------------------------------
#  cleanup_notified_badge_notifications
# ---------------------------------------------------------------------------

class TestCleanupNotifiedBadgeNotifications:
    def test_removes_old_notified_badges(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("buser", generate_password_hash("pw"), role="user")
        bt_id = db.create_badge_type("TestBadge", "desc", "🏅")
        db.award_badge(uid, bt_id, awarded_by=uid)
        db.mark_badges_notified(uid)
        # Backdate the notification
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE badge_notifications SET created_at = datetime('now', '-30 days') "
            "WHERE user_id = ?",
            (uid,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_notified_badge_notifications(retention_days=7)
        assert removed == 1

    def test_keeps_unnotified_badges(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("buser2", generate_password_hash("pw"), role="user")
        bt_id = db.create_badge_type("TestBadge2", "desc", "🏅")
        db.award_badge(uid, bt_id, awarded_by=uid)
        # Don't mark as notified

        removed = db.cleanup_notified_badge_notifications(retention_days=7)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_released_reservations
# ---------------------------------------------------------------------------

class TestCleanupReleasedReservations:
    def test_removes_old_released_reservations(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ruser", generate_password_hash("pw"), role="editor")
        pid = db.create_page("ResPage", "res-page", "content", None, uid)
        # Insert a released reservation directly
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO page_reservations (page_id, user_id, reserved_at, expires_at, released_at) "
            "VALUES (?, ?, datetime('now', '-90 days'), datetime('now', '-88 days'), datetime('now', '-88 days'))",
            (pid, uid),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_released_reservations(retention_days=30)
        assert removed == 1

    def test_keeps_active_reservations(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ruser2", generate_password_hash("pw"), role="editor")
        pid = db.create_page("ResPage2", "res-page-2", "content", None, uid)
        # Insert an active (not released) reservation
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO page_reservations (page_id, user_id, reserved_at, expires_at) "
            "VALUES (?, ?, datetime('now'), datetime('now', '+2 days'))",
            (pid, uid),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_released_reservations(retention_days=30)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_old_login_attempts
# ---------------------------------------------------------------------------

class TestCleanupOldLoginAttempts:
    def test_removes_old_login_attempts(self, isolated_db):
        import db
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO login_attempts (ip, attempted_at) "
            "VALUES ('1.2.3.4', datetime('now', '-30 days'))"
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_old_login_attempts(retention_days=7)
        assert removed == 1

    def test_keeps_recent_login_attempts(self, isolated_db):
        import db
        db.record_login_attempt("1.2.3.4")

        removed = db.cleanup_old_login_attempts(retention_days=7)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_soft_deleted_messages
# ---------------------------------------------------------------------------

class TestCleanupSoftDeletedMessages:
    def test_removes_old_soft_deleted_dm(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        u1 = db.create_user("chat1", generate_password_hash("pw"), role="user")
        u2 = db.create_user("chat2", generate_password_hash("pw"), role="user")
        chat = db.get_or_create_chat(u1, u2)
        msg_id = db.send_chat_message(chat["id"], u1, "will be deleted")
        db.delete_chat_message(msg_id)
        # Backdate the deletion
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE chat_messages SET deleted_at = datetime('now', '-30 days') WHERE id = ?",
            (msg_id,),
        )
        conn.commit()
        conn.close()

        result = db.cleanup_soft_deleted_messages(retention_days=7)
        assert result["dm"] == 1

    def test_removes_old_soft_deleted_group(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("guser", generate_password_hash("pw"), role="user")
        group = db.create_group_chat("TestGroup", uid)
        gid = group["id"]
        msg_id = db.send_group_message(gid, uid, "group msg", ip_address="127.0.0.1")
        db.delete_group_message(msg_id)
        # Backdate the deletion
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE group_messages SET deleted_at = datetime('now', '-30 days') WHERE id = ?",
            (msg_id,),
        )
        conn.commit()
        conn.close()

        result = db.cleanup_soft_deleted_messages(retention_days=7)
        assert result["group"] == 1

    def test_keeps_recently_deleted_messages(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        u1 = db.create_user("chat3", generate_password_hash("pw"), role="user")
        u2 = db.create_user("chat4", generate_password_hash("pw"), role="user")
        chat = db.get_or_create_chat(u1, u2)
        msg_id = db.send_chat_message(chat["id"], u1, "recently deleted")
        db.delete_chat_message(msg_id)

        result = db.cleanup_soft_deleted_messages(retention_days=7)
        assert result["dm"] == 0


# ---------------------------------------------------------------------------
#  optimize_database
# ---------------------------------------------------------------------------

class TestOptimizeDatabase:
    def test_optimize_runs_without_error(self, isolated_db):
        import db
        # Should not raise
        db.optimize_database()


# ---------------------------------------------------------------------------
#  run_full_cleanup
# ---------------------------------------------------------------------------

class TestRunFullCleanup:
    def test_full_cleanup_returns_summary(self, isolated_db):
        import db
        summary = db.run_full_cleanup()
        assert "expired_invite_codes" in summary
        assert "stale_drafts" in summary
        assert "notified_badge_notifications" in summary
        assert "released_reservations" in summary
        assert "old_login_attempts" in summary
        assert "soft_deleted_messages" in summary
        assert summary["optimized"] is True

    def test_full_cleanup_without_vacuum(self, isolated_db):
        import db
        summary = db.run_full_cleanup(vacuum=False)
        assert summary["optimized"] is False

    def test_full_cleanup_removes_stale_data(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("full_admin", generate_password_hash("pw"), role="admin")
        db.update_site_settings(setup_done=1)

        # Create an old expired invite
        code = db.generate_invite_code(uid)
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE invite_codes SET created_at = datetime('now', '-60 days'), "
            "expires_at = datetime('now', '-59 days') WHERE code = ?",
            (code,),
        )
        conn.commit()
        conn.close()

        # Create an old login attempt
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO login_attempts (ip, attempted_at) "
            "VALUES ('1.2.3.4', datetime('now', '-30 days'))"
        )
        conn.commit()
        conn.close()

        summary = db.run_full_cleanup()
        assert summary["expired_invite_codes"] >= 1
        assert summary["old_login_attempts"] >= 1


# ---------------------------------------------------------------------------
#  get_all_referenced_image_filenames (expanded scanning)
# ---------------------------------------------------------------------------

class TestGetAllReferencedImageFilenames:
    def test_finds_images_in_drafts(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ref_u", generate_password_hash("pw"), role="editor")
        pid = db.create_page("RefPage", "ref-page", "no images", None, uid)
        db.save_draft(pid, uid, "Draft with image", "![img](/static/uploads/draft_img.png)")

        filenames = db.get_all_referenced_image_filenames()
        assert "draft_img.png" in filenames

    def test_finds_images_in_announcements(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ann_u", generate_password_hash("pw"), role="admin")
        db.create_announcement(
            content="![banner](/static/uploads/ann_img.png)",
            color="red",
            text_size="normal",
            visibility="both",
            expires_at=None,
            user_id=uid,
        )

        filenames = db.get_all_referenced_image_filenames()
        assert "ann_img.png" in filenames

    def test_still_finds_page_and_history_images(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ph_u", generate_password_hash("pw"), role="editor")
        pid = db.create_page("PHPage", "ph-page",
                             "![img](/static/uploads/page_img.png)", None, uid)
        db.update_page(pid, "PHPage", "![img](/static/uploads/new_img.png)", uid, "update")

        filenames = db.get_all_referenced_image_filenames()
        assert "page_img.png" in filenames  # in history
        assert "new_img.png" in filenames  # in current page


# ---------------------------------------------------------------------------
#  cleanup_orphaned_attachments
# ---------------------------------------------------------------------------

class TestCleanupOrphanedAttachments:
    def test_removes_orphaned_attachment_files(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_orphaned_attachments
        import config as cfg
        att_dir = str(tmp_path / "attachments")
        os.makedirs(att_dir, exist_ok=True)
        # Put an orphan file on disk with no DB record
        orphan = os.path.join(att_dir, "orphan.pdf")
        with open(orphan, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "ATTACHMENT_FOLDER", None)
        cfg.ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_attachments()
        finally:
            cfg.ATTACHMENT_FOLDER = original

        assert removed == 1
        assert not os.path.isfile(orphan)

    def test_keeps_known_attachment_files(self, isolated_db, tmp_path):
        import db
        from werkzeug.security import generate_password_hash
        from routes.uploads import cleanup_orphaned_attachments
        import config as cfg

        uid = db.create_user("att_u", generate_password_hash("pw"), role="editor")
        pid = db.create_page("AttPage", "att-page", "content", None, uid)
        db.add_page_attachment(pid, "known.pdf", "known.pdf", 100, uid)

        att_dir = str(tmp_path / "attachments")
        os.makedirs(att_dir, exist_ok=True)
        known = os.path.join(att_dir, "known.pdf")
        with open(known, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "ATTACHMENT_FOLDER", None)
        cfg.ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_attachments()
        finally:
            cfg.ATTACHMENT_FOLDER = original

        assert removed == 0
        assert os.path.isfile(known)

    def test_skips_dotfiles(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_orphaned_attachments
        import config as cfg

        att_dir = str(tmp_path / "attachments")
        os.makedirs(att_dir, exist_ok=True)
        dotfile = os.path.join(att_dir, ".gitkeep")
        with open(dotfile, "wb") as f:
            f.write(b"")

        original = getattr(cfg, "ATTACHMENT_FOLDER", None)
        cfg.ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_attachments()
        finally:
            cfg.ATTACHMENT_FOLDER = original

        assert removed == 0
        assert os.path.isfile(dotfile)


# ---------------------------------------------------------------------------
#  cleanup_stale_upload_msg_store (sync.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  cleanup_expired_announcements
# ---------------------------------------------------------------------------

class TestCleanupExpiredAnnouncements:
    def test_removes_old_expired_announcements(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ann_admin", generate_password_hash("pw"), role="admin")
        ann_id = db.create_announcement(
            content="Old expired", color="red", text_size="normal",
            visibility="both", expires_at="2020-01-01T00:00:00",
            user_id=uid,
        )
        # Backdate so it's old enough
        _backdate("announcements", "created_at", ann_id, 60)

        removed = db.cleanup_expired_announcements(retention_days=30)
        assert removed == 1

    def test_keeps_active_announcements(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ann_admin2", generate_password_hash("pw"), role="admin")
        db.create_announcement(
            content="Still active", color="blue", text_size="normal",
            visibility="both", expires_at=None,
            user_id=uid,
        )

        removed = db.cleanup_expired_announcements(retention_days=30)
        assert removed == 0

    def test_removes_old_inactive_announcements(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ann_admin3", generate_password_hash("pw"), role="admin")
        ann_id = db.create_announcement(
            content="Inactive old", color="green", text_size="normal",
            visibility="both", expires_at=None,
            user_id=uid,
        )
        # Deactivate and backdate
        db.update_announcement(ann_id, is_active=0)
        _backdate("announcements", "created_at", ann_id, 60)

        removed = db.cleanup_expired_announcements(retention_days=30)
        assert removed == 1

    def test_keeps_recent_inactive_announcements(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("ann_admin4", generate_password_hash("pw"), role="admin")
        ann_id = db.create_announcement(
            content="Just deactivated", color="orange", text_size="normal",
            visibility="both", expires_at=None,
            user_id=uid,
        )
        db.update_announcement(ann_id, is_active=0)
        # Don't backdate: should be kept

        removed = db.cleanup_expired_announcements(retention_days=30)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_resolved_quota_requests
# ---------------------------------------------------------------------------

class TestCleanupResolvedQuotaRequests:
    def test_removes_old_approved_requests(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("qr_user", generate_password_hash("pw"), role="editor")
        admin_id = db.create_user("qr_admin", generate_password_hash("pw"), role="admin")
        # Insert a resolved request directly for test control
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO reservation_quota_requests "
            "(user_id, requested_quota, reason, status, reviewed_by, reviewed_at, created_at) "
            "VALUES (?, 5, 'Need more', 'approved', ?, datetime('now'), datetime('now', '-120 days'))",
            (uid, admin_id),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_resolved_quota_requests(retention_days=90)
        assert removed == 1

    def test_keeps_pending_requests(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("qr_user2", generate_password_hash("pw"), role="editor")
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO reservation_quota_requests "
            "(user_id, requested_quota, reason, status, created_at) "
            "VALUES (?, 5, 'Need more', 'pending', datetime('now'))",
            (uid,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_resolved_quota_requests(retention_days=90)
        assert removed == 0

    def test_removes_old_denied_requests(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("qr_user3", generate_password_hash("pw"), role="editor")
        admin_id = db.create_user("qr_admin2", generate_password_hash("pw"), role="admin")
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO reservation_quota_requests "
            "(user_id, requested_quota, reason, status, reviewed_by, reviewed_at, created_at) "
            "VALUES (?, 10, 'More pages', 'denied', ?, datetime('now'), datetime('now', '-120 days'))",
            (uid, admin_id),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_resolved_quota_requests(retention_days=90)
        assert removed == 1

    def test_keeps_recent_resolved_requests(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("qr_user4", generate_password_hash("pw"), role="editor")
        admin_id = db.create_user("qr_admin3", generate_password_hash("pw"), role="admin")
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO reservation_quota_requests "
            "(user_id, requested_quota, reason, status, reviewed_by, reviewed_at, created_at) "
            "VALUES (?, 5, 'Need more', 'approved', ?, datetime('now'), datetime('now'))",
            (uid, admin_id),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_resolved_quota_requests(retention_days=90)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_revoked_badges
# ---------------------------------------------------------------------------

class TestCleanupRevokedBadges:
    def test_removes_old_revoked_badges(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rb_user", generate_password_hash("pw"), role="user")
        admin_id = db.create_user("rb_admin", generate_password_hash("pw"), role="admin")
        bt_id = db.create_badge_type("RevBadge", "desc", "🏅")
        db.award_badge(uid, bt_id, awarded_by=admin_id)
        db.revoke_badge(uid, bt_id, revoked_by=admin_id)
        # Backdate the revocation
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE user_badges SET revoked_at = datetime('now', '-120 days') "
            "WHERE user_id = ? AND badge_type_id = ?",
            (uid, bt_id),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_revoked_badges(retention_days=90)
        assert removed == 1

    def test_keeps_active_badges(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rb_user2", generate_password_hash("pw"), role="user")
        bt_id = db.create_badge_type("ActiveBadge", "desc", "🏅")
        db.award_badge(uid, bt_id, awarded_by=uid)

        removed = db.cleanup_revoked_badges(retention_days=90)
        assert removed == 0

    def test_keeps_recently_revoked_badges(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rb_user3", generate_password_hash("pw"), role="user")
        admin_id = db.create_user("rb_admin2", generate_password_hash("pw"), role="admin")
        bt_id = db.create_badge_type("RecentRevBadge", "desc", "🏅")
        db.award_badge(uid, bt_id, awarded_by=admin_id)
        db.revoke_badge(uid, bt_id, revoked_by=admin_id)
        # Don't backdate: recent, should be kept

        removed = db.cleanup_revoked_badges(retention_days=90)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_old_role_history
# ---------------------------------------------------------------------------

class TestCleanupOldRoleHistory:
    def test_removes_old_role_history(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rh_user", generate_password_hash("pw"), role="editor")
        admin_id = db.create_user("rh_admin", generate_password_hash("pw"), role="admin")
        db.record_role_change(uid, "user", "editor", changed_by=admin_id)
        # Backdate beyond 365 days
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE role_history SET changed_at = datetime('now', '-400 days') "
            "WHERE user_id = ?",
            (uid,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_old_role_history(retention_days=365)
        assert removed == 1

    def test_keeps_recent_role_history(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("rh_user2", generate_password_hash("pw"), role="editor")
        admin_id = db.create_user("rh_admin2", generate_password_hash("pw"), role="admin")
        db.record_role_change(uid, "user", "editor", changed_by=admin_id)

        removed = db.cleanup_old_role_history(retention_days=365)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_old_username_history
# ---------------------------------------------------------------------------

class TestCleanupOldUsernameHistory:
    def test_removes_old_username_history(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("uh_user", generate_password_hash("pw"), role="user")
        db.record_username_change(uid, "uh_user", "uh_user_new")
        # Backdate beyond 365 days
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE username_history SET changed_at = datetime('now', '-400 days') "
            "WHERE user_id = ?",
            (uid,),
        )
        conn.commit()
        conn.close()

        removed = db.cleanup_old_username_history(retention_days=365)
        assert removed == 1

    def test_keeps_recent_username_history(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("uh_user2", generate_password_hash("pw"), role="user")
        db.record_username_change(uid, "uh_user2", "uh_user2_new")

        removed = db.cleanup_old_username_history(retention_days=365)
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_expired_group_timeouts
# ---------------------------------------------------------------------------

class TestCleanupExpiredGroupTimeouts:
    def test_clears_expired_group_timeouts(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("gt_user", generate_password_hash("pw"), role="user")
        group = db.create_group_chat("TimeoutGroup", uid)
        gid = group["id"]
        # Set an expired timeout
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE group_members SET timed_out_until = datetime('now', '-1 hour') "
            "WHERE group_id = ? AND user_id = ?",
            (gid, uid),
        )
        conn.commit()
        conn.close()

        cleared = db.cleanup_expired_group_timeouts()
        assert cleared == 1

        # Verify the timeout was cleared
        member = db.get_group_member(gid, uid)
        assert member["timed_out_until"] is None

    def test_keeps_future_group_timeouts(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("gt_user2", generate_password_hash("pw"), role="user")
        group = db.create_group_chat("TimeoutGroup2", uid)
        gid = group["id"]
        # Set a future timeout
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE group_members SET timed_out_until = datetime('now', '+1 day') "
            "WHERE group_id = ? AND user_id = ?",
            (gid, uid),
        )
        conn.commit()
        conn.close()

        cleared = db.cleanup_expired_group_timeouts()
        assert cleared == 0

    def test_no_op_when_no_timeouts(self, isolated_db):
        import db
        cleared = db.cleanup_expired_group_timeouts()
        assert cleared == 0


# ---------------------------------------------------------------------------
#  run_full_cleanup includes new routines
# ---------------------------------------------------------------------------

class TestRunFullCleanupExpanded:
    def test_full_cleanup_includes_new_keys(self, isolated_db):
        import db
        summary = db.run_full_cleanup()
        assert "expired_announcements" in summary
        assert "resolved_quota_requests" in summary
        assert "revoked_badges" in summary
        assert "old_role_history" in summary
        assert "old_username_history" in summary
        assert "expired_group_timeouts" in summary
        # Also check original keys still present
        assert "expired_invite_codes" in summary
        assert "stale_drafts" in summary
        assert "optimized" in summary

    def test_full_cleanup_cleans_multiple_categories(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("fc_admin", generate_password_hash("pw"), role="admin")
        db.update_site_settings(setup_done=1)

        # Create old expired announcement
        ann_id = db.create_announcement(
            content="Old", color="red", text_size="normal",
            visibility="both", expires_at="2020-01-01T00:00:00",
            user_id=uid,
        )
        _backdate("announcements", "created_at", ann_id, 60)

        # Create old role history
        db.record_role_change(uid, "user", "admin", changed_by=uid)
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "UPDATE role_history SET changed_at = datetime('now', '-400 days') "
            "WHERE user_id = ?", (uid,)
        )
        conn.commit()
        conn.close()

        summary = db.run_full_cleanup()
        assert summary["expired_announcements"] >= 1
        assert summary["old_role_history"] >= 1


# ---------------------------------------------------------------------------
#  get_all_referenced_image_filenames includes custom pages
# ---------------------------------------------------------------------------

class TestGetAllReferencedImageFilenamesCustomPages:
    def test_finds_images_in_custom_page_content(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("cp_u", generate_password_hash("pw"), role="admin")
        db.create_custom_page(
            path="/custom-test",
            title="Custom Test",
            content_type="html",
            content='<img src="/static/uploads/custom_img.png">',
            created_by=uid,
        )

        filenames = db.get_all_referenced_image_filenames()
        assert "custom_img.png" in filenames

    def test_finds_images_in_custom_page_css(self, isolated_db):
        import db
        from werkzeug.security import generate_password_hash
        uid = db.create_user("cp_u2", generate_password_hash("pw"), role="admin")
        db.create_custom_page(
            path="/custom-css",
            title="CSS Test",
            content_type="html",
            content="no images here",
            css='background: url("/static/uploads/bg_img.png")',
            created_by=uid,
        )

        filenames = db.get_all_referenced_image_filenames()
        assert "bg_img.png" in filenames


# ---------------------------------------------------------------------------
#  cleanup_orphaned_chat_attachments
# ---------------------------------------------------------------------------

class TestCleanupOrphanedChatAttachments:
    def test_removes_orphaned_chat_attachment_files(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_orphaned_chat_attachments
        import config as cfg
        att_dir = str(tmp_path / "chat_attachments")
        os.makedirs(att_dir, exist_ok=True)
        # Put an orphan file on disk with no DB record
        orphan = os.path.join(att_dir, "orphan_chat.pdf")
        with open(orphan, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "CHAT_ATTACHMENT_FOLDER", None)
        cfg.CHAT_ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_chat_attachments()
        finally:
            cfg.CHAT_ATTACHMENT_FOLDER = original

        assert removed == 1
        assert not os.path.isfile(orphan)

    def test_keeps_known_chat_attachment_files(self, isolated_db, tmp_path):
        import db
        from werkzeug.security import generate_password_hash
        from routes.uploads import cleanup_orphaned_chat_attachments
        import config as cfg

        u1 = db.create_user("ca_u1", generate_password_hash("pw"), role="user")
        u2 = db.create_user("ca_u2", generate_password_hash("pw"), role="user")
        chat = db.get_or_create_chat(u1, u2)
        msg_id = db.send_chat_message(chat["id"], u1, "with attachment")
        db.add_chat_attachment(msg_id, "known_chat.pdf", "known_chat.pdf", 100)

        att_dir = str(tmp_path / "chat_attachments")
        os.makedirs(att_dir, exist_ok=True)
        known = os.path.join(att_dir, "known_chat.pdf")
        with open(known, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "CHAT_ATTACHMENT_FOLDER", None)
        cfg.CHAT_ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_chat_attachments()
        finally:
            cfg.CHAT_ATTACHMENT_FOLDER = original

        assert removed == 0
        assert os.path.isfile(known)

    def test_skips_dotfiles(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_orphaned_chat_attachments
        import config as cfg
        att_dir = str(tmp_path / "chat_attachments")
        os.makedirs(att_dir, exist_ok=True)
        dotfile = os.path.join(att_dir, ".gitkeep")
        with open(dotfile, "wb") as f:
            f.write(b"")

        original = getattr(cfg, "CHAT_ATTACHMENT_FOLDER", None)
        cfg.CHAT_ATTACHMENT_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_chat_attachments()
        finally:
            cfg.CHAT_ATTACHMENT_FOLDER = original

        assert removed == 0
        assert os.path.isfile(dotfile)

    def test_returns_zero_when_no_folder(self, isolated_db):
        from routes.uploads import cleanup_orphaned_chat_attachments
        import config as cfg
        original = getattr(cfg, "CHAT_ATTACHMENT_FOLDER", None)
        cfg.CHAT_ATTACHMENT_FOLDER = "/nonexistent/path"
        try:
            removed = cleanup_orphaned_chat_attachments()
        finally:
            cfg.CHAT_ATTACHMENT_FOLDER = original
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_orphaned_custom_page_files
# ---------------------------------------------------------------------------

class TestCleanupOrphanedCustomPageFiles:
    def test_removes_orphaned_custom_page_files(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_orphaned_custom_page_files
        import config as cfg
        att_dir = str(tmp_path / "custom_page_files")
        os.makedirs(att_dir, exist_ok=True)
        # Put an orphan file on disk with no DB record
        orphan = os.path.join(att_dir, "orphan_custom.pdf")
        with open(orphan, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "CUSTOM_PAGE_FILES_FOLDER", None)
        cfg.CUSTOM_PAGE_FILES_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_custom_page_files()
        finally:
            if original is not None:
                cfg.CUSTOM_PAGE_FILES_FOLDER = original

        assert removed == 1
        assert not os.path.isfile(orphan)

    def test_keeps_known_custom_page_files(self, isolated_db, tmp_path):
        import db
        from werkzeug.security import generate_password_hash
        from routes.uploads import cleanup_orphaned_custom_page_files
        import config as cfg

        uid = db.create_user("cpf_u", generate_password_hash("pw"), role="admin")
        page_id = db.create_custom_page(
            path="/cpf-test",
            title="CPF Test",
            content_type="html",
            content="test",
            created_by=uid,
        )
        db.add_custom_page_file(page_id, "known_custom.pdf", "known_custom.pdf",
                                "application/pdf", 100)

        att_dir = str(tmp_path / "custom_page_files")
        os.makedirs(att_dir, exist_ok=True)
        known = os.path.join(att_dir, "known_custom.pdf")
        with open(known, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "CUSTOM_PAGE_FILES_FOLDER", None)
        cfg.CUSTOM_PAGE_FILES_FOLDER = att_dir
        try:
            removed = cleanup_orphaned_custom_page_files()
        finally:
            if original is not None:
                cfg.CUSTOM_PAGE_FILES_FOLDER = original

        assert removed == 0
        assert os.path.isfile(known)

    def test_returns_zero_when_no_folder(self, isolated_db):
        from routes.uploads import cleanup_orphaned_custom_page_files
        import config as cfg
        original = getattr(cfg, "CUSTOM_PAGE_FILES_FOLDER", None)
        cfg.CUSTOM_PAGE_FILES_FOLDER = "/nonexistent/path"
        try:
            removed = cleanup_orphaned_custom_page_files()
        finally:
            if original is not None:
                cfg.CUSTOM_PAGE_FILES_FOLDER = original
        assert removed == 0


# ---------------------------------------------------------------------------
#  cleanup_unused_uploads returns count
# ---------------------------------------------------------------------------

class TestCleanupUnusedUploadsReturnValue:
    def test_returns_removed_count(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_unused_uploads
        import config as cfg

        upload_dir = str(tmp_path / "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        orphan = os.path.join(upload_dir, "orphan_upload.png")
        with open(orphan, "wb") as f:
            f.write(b"fake")

        original = getattr(cfg, "UPLOAD_FOLDER", None)
        cfg.UPLOAD_FOLDER = upload_dir
        try:
            removed = cleanup_unused_uploads()
        finally:
            cfg.UPLOAD_FOLDER = original

        assert removed == 1
        assert not os.path.isfile(orphan)

    def test_returns_zero_when_nothing_to_clean(self, isolated_db, tmp_path):
        from routes.uploads import cleanup_unused_uploads
        import config as cfg

        upload_dir = str(tmp_path / "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        original = getattr(cfg, "UPLOAD_FOLDER", None)
        cfg.UPLOAD_FOLDER = upload_dir
        try:
            removed = cleanup_unused_uploads()
        finally:
            cfg.UPLOAD_FOLDER = original

        assert removed == 0


# ---------------------------------------------------------------------------
#  notify_cleanup_completed (sync.py)
# ---------------------------------------------------------------------------


class TestRetentionDaysValidation:
    """Ensure every cleanup function rejects retention_days < 1."""

    @pytest.mark.parametrize("bad_value", [-1, 0, -100])
    @pytest.mark.parametrize("func_name", [
        "cleanup_expired_invite_codes",
        "cleanup_stale_drafts",
        "cleanup_notified_badge_notifications",
        "cleanup_released_reservations",
        "cleanup_old_login_attempts",
        "cleanup_old_rate_limit_hits",
        "cleanup_soft_deleted_messages",
        "cleanup_expired_announcements",
        "cleanup_resolved_quota_requests",
        "cleanup_revoked_badges",
        "cleanup_old_role_history",
        "cleanup_old_username_history",
    ])
    def test_rejects_non_positive_retention_days(self, isolated_db, func_name, bad_value):
        import db
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            getattr(db, func_name)(retention_days=bad_value)

    def test_run_full_cleanup_rejects_negative(self, isolated_db):
        import db
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            db.run_full_cleanup(invite_retention_days=-1)

    def test_accepts_retention_days_of_one(self, isolated_db):
        import db
        # Should not raise: 1 is the minimum valid value
        db.cleanup_stale_drafts(retention_days=1)
        db.cleanup_old_login_attempts(retention_days=1)
        db.cleanup_old_rate_limit_hits(retention_days=1)
