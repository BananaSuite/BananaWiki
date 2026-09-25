"""Additional edge case tests for diff, time, audit, and invite helpers.

Covers behavioral gaps left after previous test passes:
- helpers/_diff.py: compute_diff_html / compute_formatted_diff_html output
- helpers/_time.py: format_datetime_local_input, get_effective_chat_cleanup_settings,
                    get_next_chat_cleanup_time, get_time_since/until_chat_cleanup
- db/_audit.py: deattribute_contribution, mass_reattribute_contributions,
                delete_role_history_entry, delete_all_role_history, get_role_history_entry
- db/_invites.py: hard_delete_invite_code, suspended-creator rejection, expired code rejection
"""

import pytest


# ---------------------------------------------------------------------------
# helpers/_diff.py
# ---------------------------------------------------------------------------

class TestComputeDiffHtml:
    """Behavioral tests for compute_diff_html."""

    def _call(self, old, new):
        from helpers._diff import compute_diff_html
        return str(compute_diff_html(old, new))

    def test_identical_text_has_no_highlights(self):
        result = self._call("hello world", "hello world")
        assert "diff-ins" not in result
        assert "diff-del" not in result

    def test_inserted_word_wrapped_with_ins(self):
        result = self._call("hello", "hello world")
        assert "diff-ins" in result
        assert "world" in result

    def test_deleted_word_wrapped_with_del(self):
        result = self._call("hello world", "hello")
        assert "diff-del" in result
        assert "world" in result

    def test_replaced_word_has_both_del_and_ins(self):
        result = self._call("cat", "dog")
        assert "diff-del" in result
        assert "diff-ins" in result

    def test_output_wrapped_in_pre(self):
        result = self._call("a", "b")
        assert result.startswith('<pre class="diff-pre">')
        assert result.endswith("</pre>")

    def test_empty_old_text(self):
        result = self._call("", "new content")
        # Word-level diff wraps each word individually in <ins>
        assert "diff-ins" in result
        assert "new" in result
        assert "content" in result

    def test_empty_new_text(self):
        result = self._call("old content", "")
        # Word-level diff wraps each word individually in <del>
        assert "diff-del" in result
        assert "old" in result
        assert "content" in result

    def test_both_empty(self):
        result = self._call("", "")
        assert "diff-ins" not in result
        assert "diff-del" not in result

    def test_none_treated_as_empty(self):
        result = self._call(None, "hello")
        assert "hello" in result
        assert "diff-ins" in result

    def test_html_entities_escaped(self):
        result = self._call("a < b", "a > b")
        # Raw < and > should not appear unescaped in non-tag positions
        assert "<pre" in result  # the wrapping pre tag is fine

    def test_whitespace_only_changes_not_highlighted(self):
        result = self._call("hello world", "hello  world")
        # The extra space itself (whitespace) should not be wrapped in ins/del
        assert "diff-ins" not in result and "diff-del" not in result


class TestComputeFormattedDiffHtml:
    """Behavioral tests for compute_formatted_diff_html."""

    def _call(self, old, new):
        from helpers._diff import compute_formatted_diff_html
        return str(compute_formatted_diff_html(old, new))

    def test_identical_markdown_no_highlights(self):
        text = "# Heading\n\nSome paragraph."
        result = self._call(text, text)
        assert "diff-ins" not in result
        assert "diff-del" not in result

    def test_added_text_marked_with_ins(self):
        result = self._call("# Hello", "# Hello World")
        assert "diff-ins" in result

    def test_deleted_text_marked_with_del(self):
        result = self._call("# Hello World", "# Hello")
        assert "diff-del" in result

    def test_empty_old_renders_new(self):
        result = self._call("", "# Title\n\nContent")
        assert "Title" in result

    def test_empty_new_emits_deletions(self):
        result = self._call("# Title\n\nContent", "")
        assert "diff-del" in result

    def test_both_empty(self):
        result = self._call("", "")
        assert "diff-ins" not in result
        assert "diff-del" not in result

    def test_returns_markup_safe(self):
        from markupsafe import Markup
        from helpers._diff import compute_formatted_diff_html
        result = compute_formatted_diff_html("old", "new")
        assert isinstance(result, Markup)


# ---------------------------------------------------------------------------
# helpers/_time.py
# ---------------------------------------------------------------------------

class TestFormatDatetimeLocalInput:
    """Tests for format_datetime_local_input."""

    def test_valid_datetime_formatted(self):
        from helpers._time import format_datetime_local_input
        result = format_datetime_local_input("2026-06-15T10:00:00")
        # Should be in YYYY-MM-DDTHH:MM format (for datetime-local input)
        assert "T" in result
        assert len(result) == 16  # e.g. "2026-06-15T10:00"

    def test_none_returns_empty(self):
        from helpers._time import format_datetime_local_input
        assert format_datetime_local_input(None) == ""

    def test_empty_string_returns_empty(self):
        from helpers._time import format_datetime_local_input
        assert format_datetime_local_input("") == ""

    def test_invalid_string_returns_empty(self):
        from helpers._time import format_datetime_local_input
        assert format_datetime_local_input("not-a-date") == ""

    def test_timezone_aware_input_converted(self):
        from helpers._time import format_datetime_local_input
        import re
        result = format_datetime_local_input("2026-01-01T00:00:00+00:00")
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", result), (
            f"Expected YYYY-MM-DDTHH:MM format, got: {result!r}"
        )


class TestGetEffectiveChatCleanupSettings:
    """Tests for get_effective_chat_cleanup_settings."""

    def test_none_settings_returns_safe_defaults(self):
        from helpers._time import get_effective_chat_cleanup_settings
        result = get_effective_chat_cleanup_settings(None)
        assert result["dm"]["auto_clear_messages"] == 0
        assert result["group"]["auto_clear_messages"] == 0

    def test_legacy_values_used_when_split_not_configured(self):
        from helpers._time import get_effective_chat_cleanup_settings
        settings = {
            "chat_auto_clear_messages": 1,
            "chat_auto_clear_attachments": 1,
            "chat_message_retention_days": 14,
            "chat_attachment_retention_days": 7,
            "chat_dm_auto_clear_messages": 0,        # migration default
            "chat_dm_auto_clear_attachments": 1,     # migration default
            "chat_dm_message_retention_days": 0,     # migration default
            "chat_dm_attachment_retention_days": 7,  # migration default
            "chat_group_auto_clear_messages": 0,
            "chat_group_auto_clear_attachments": 1,
            "chat_group_message_retention_days": 0,
            "chat_group_attachment_retention_days": 7,
            "chat_cleanup_split_configured": 0,
        }
        result = get_effective_chat_cleanup_settings(settings)
        # Legacy auto_clear_messages=1 should be used for dm scope
        assert result["dm"]["auto_clear_messages"] == 1

    def test_split_configured_flag_overrides_legacy(self):
        from helpers._time import get_effective_chat_cleanup_settings
        settings = {
            "chat_auto_clear_messages": 1,
            "chat_auto_clear_attachments": 1,
            "chat_message_retention_days": 14,
            "chat_attachment_retention_days": 7,
            "chat_dm_auto_clear_messages": 0,
            "chat_dm_auto_clear_attachments": 0,
            "chat_dm_message_retention_days": 30,
            "chat_dm_attachment_retention_days": 14,
            "chat_group_auto_clear_messages": 1,
            "chat_group_auto_clear_attachments": 1,
            "chat_group_message_retention_days": 7,
            "chat_group_attachment_retention_days": 3,
            "chat_cleanup_split_configured": 1,
        }
        result = get_effective_chat_cleanup_settings(settings)
        # split configured: use DM-specific values
        assert result["dm"]["auto_clear_messages"] == 0
        assert result["dm"]["message_retention_days"] == 30
        assert result["group"]["auto_clear_messages"] == 1

    def test_missing_split_configured_key_treated_as_false(self):
        from helpers._time import get_effective_chat_cleanup_settings
        settings = {
            "chat_auto_clear_messages": 1,
            "chat_auto_clear_attachments": 1,
            "chat_message_retention_days": 14,
            "chat_attachment_retention_days": 7,
            "chat_dm_auto_clear_messages": 0,
            "chat_dm_auto_clear_attachments": 1,
            "chat_dm_message_retention_days": 0,
            "chat_dm_attachment_retention_days": 7,
            "chat_group_auto_clear_messages": 0,
            "chat_group_auto_clear_attachments": 1,
            "chat_group_message_retention_days": 0,
            "chat_group_attachment_retention_days": 7,
            # no chat_cleanup_split_configured key
        }
        result = get_effective_chat_cleanup_settings(settings)
        # Should not raise; should fall back to legacy
        assert "dm" in result
        assert "group" in result


class TestGetTimeSinceAndUntilChatCleanup:
    """Tests for get_time_since_last_chat_cleanup and get_time_until_next_chat_cleanup."""

    def test_time_since_returns_string_when_no_cleanup_recorded(self, admin_user):
        from helpers._time import get_time_since_last_chat_cleanup
        result = get_time_since_last_chat_cleanup()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_time_until_returns_string(self, admin_user):
        from helpers._time import get_time_until_next_chat_cleanup
        result = get_time_until_next_chat_cleanup()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_time_since_no_settings_fallback(self):
        from helpers._time import get_time_since_last_chat_cleanup
        # Even with no database, it should return a string not raise
        result = get_time_since_last_chat_cleanup()
        assert isinstance(result, str)


class TestGetNextChatCleanupTime:
    """Tests for get_next_chat_cleanup_time."""

    def test_returns_iso_string_or_none(self, admin_user):
        from helpers._time import get_next_chat_cleanup_time
        result = get_next_chat_cleanup_time()
        # Should return an ISO string when DB is available
        assert result is not None
        assert "T" in result

    def test_result_is_in_future(self, admin_user):
        from helpers._time import get_next_chat_cleanup_time
        from datetime import datetime, timezone
        result = get_next_chat_cleanup_time()
        assert result is not None
        next_time = datetime.fromisoformat(result)
        assert next_time > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# db/_audit.py
# ---------------------------------------------------------------------------

class TestDeattributeContribution:
    """Tests for deattribute_contribution and deattribute_all_user_contributions."""

    def test_deattribute_single_contribution(self, admin_user, editor_user):
        import db
        cat_id = db.create_category("Cat")
        page_id = db.create_page("Page", "page", "v1", cat_id, editor_user)
        db.update_page(page_id, "Page", "v2", editor_user, "edit")
        history = db.get_page_history(page_id)
        entry_id = history[0]["id"]
        assert history[0]["edited_by"] == editor_user

        db.deattribute_contribution(entry_id)

        history_after = db.get_page_history(page_id)
        for entry in history_after:
            if entry["id"] == entry_id:
                assert entry["edited_by"] is None
                break

    def test_deattribute_all_user_contributions(self, admin_user, editor_user):
        import db
        cat_id = db.create_category("Cat")
        page_id = db.create_page("Page", "page", "v1", cat_id, editor_user)
        db.update_page(page_id, "Page", "v2", editor_user, "e1")
        db.update_page(page_id, "Page", "v3", editor_user, "e2")

        count = db.deattribute_all_user_contributions(editor_user)
        assert count >= 2

        history = db.get_page_history(page_id)
        for entry in history:
            if entry["edited_by"] is not None:
                assert entry["edited_by"] != editor_user

    def test_deattribute_nonexistent_entry_is_no_op(self, admin_user):
        import db
        # Should not raise
        db.deattribute_contribution(999999)

    def test_deattribute_all_returns_zero_when_no_contributions(self, admin_user, editor_user):
        import db
        count = db.deattribute_all_user_contributions(editor_user)
        assert count == 0


class TestMassReattributeContributions:
    """Tests for mass_reattribute_contributions."""

    def test_transfers_contributions_from_one_user_to_another(self, admin_user, editor_user):
        import db
        cat_id = db.create_category("Cat")
        page_id = db.create_page("Page", "page", "v1", cat_id, editor_user)
        db.update_page(page_id, "Page", "v2", editor_user, "edit")

        count = db.mass_reattribute_contributions(editor_user, admin_user)
        assert count >= 1

        history = db.get_page_history(page_id)
        # All entries previously by editor should now be attributed to admin
        for entry in history:
            if entry["edited_by"] is not None:
                assert entry["edited_by"] == admin_user

    def test_returns_zero_when_source_has_no_contributions(self, admin_user, editor_user):
        import db
        count = db.mass_reattribute_contributions(editor_user, admin_user)
        assert count == 0

    def test_does_not_touch_unrelated_entries(self, admin_user, editor_user):
        import db
        # Create a third user
        from werkzeug.security import generate_password_hash
        third_id = db.create_user("third", generate_password_hash("pw"), role="editor")
        cat_id = db.create_category("Cat")
        page_id = db.create_page("Page", "page", "v1", cat_id, third_id)
        db.update_page(page_id, "Page", "v2", third_id, "by third")

        # Reattribute from editor (who made no contributions) to admin
        count = db.mass_reattribute_contributions(editor_user, admin_user)
        assert count == 0

        # third's entries should be untouched
        history = db.get_page_history(page_id)
        for entry in history:
            if entry["edited_by"] is not None:
                assert entry["edited_by"] == third_id


class TestDeleteRoleHistory:
    """Tests for delete_role_history_entry and delete_all_role_history."""

    def test_delete_single_role_history_entry(self, admin_user, editor_user):
        import db
        db.record_role_change(editor_user, "user", "editor", changed_by=admin_user)
        history = db.get_role_history(editor_user)
        assert len(history) >= 1
        entry_id = history[0]["id"]

        db.delete_role_history_entry(entry_id)

        history_after = db.get_role_history(editor_user)
        ids_after = [e["id"] for e in history_after]
        assert entry_id not in ids_after

    def test_delete_nonexistent_entry_is_no_op(self, admin_user):
        import db
        # Should not raise
        db.delete_role_history_entry(999999)

    def test_delete_all_role_history_removes_all(self, admin_user, editor_user):
        import db
        db.record_role_change(editor_user, "user", "editor", changed_by=admin_user)
        db.record_role_change(editor_user, "editor", "user", changed_by=admin_user)
        count = db.delete_all_role_history(editor_user)
        assert count >= 2
        assert db.get_role_history(editor_user) == []

    def test_delete_all_role_history_returns_zero_when_empty(self, admin_user, editor_user):
        import db
        count = db.delete_all_role_history(editor_user)
        assert count == 0

    def test_get_role_history_entry_returns_row(self, admin_user, editor_user):
        import db
        db.record_role_change(editor_user, "user", "editor", changed_by=admin_user)
        history = db.get_role_history(editor_user)
        entry_id = history[0]["id"]
        row = db.get_role_history_entry(entry_id)
        assert row is not None
        assert row["user_id"] == editor_user

    def test_get_role_history_entry_nonexistent_returns_none(self, admin_user):
        import db
        row = db.get_role_history_entry(999999)
        assert row is None


# ---------------------------------------------------------------------------
# db/_invites.py: edge cases
# ---------------------------------------------------------------------------

class TestInviteCodeEdgeCases:
    """Edge cases for invite code generation and validation."""

    def test_expired_code_rejected_by_validate(self, admin_user):
        import db
        from datetime import datetime, timedelta, timezone
        from db._connection import get_db

        # Create a code then manually backdate its expiry
        code = db.generate_invite_code(admin_user)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn = get_db()
        conn.execute("UPDATE invite_codes SET expires_at=? WHERE code=?", (past, code))
        conn.commit()
        conn.close()

        assert db.validate_invite_code(code) is None

    def test_used_code_rejected_by_validate(self, admin_user, editor_user):
        import db
        code = db.generate_invite_code(admin_user)
        db.use_invite_code(code, editor_user)
        assert db.validate_invite_code(code) is None

    def test_suspended_creator_code_rejected(self, admin_user, editor_user):
        import db
        code = db.generate_invite_code(admin_user)
        db.update_user(admin_user, suspended=1)
        assert db.validate_invite_code(code) is None

    def test_hard_delete_removes_code_permanently(self, admin_user):
        import db
        from db._connection import get_db
        code = db.generate_invite_code(admin_user)
        conn = get_db()
        row = conn.execute("SELECT id FROM invite_codes WHERE code=?", (code,)).fetchone()
        code_id = row["id"]
        conn.close()

        db.hard_delete_invite_code(code_id)

        conn = get_db()
        gone = conn.execute("SELECT id FROM invite_codes WHERE id=?", (code_id,)).fetchone()
        conn.close()
        assert gone is None

    def test_soft_deleted_code_not_listed_as_active(self, admin_user):
        import db
        from db._connection import get_db
        code = db.generate_invite_code(admin_user)
        conn = get_db()
        row = conn.execute("SELECT id FROM invite_codes WHERE code=?", (code,)).fetchone()
        code_id = row["id"]
        conn.close()

        db.delete_invite_code(code_id)
        active = db.list_invite_codes(active_only=True)
        active_codes = [r["code"] for r in active]
        assert code not in active_codes

    def test_soft_deleted_code_appears_in_expired_list(self, admin_user):
        import db
        from db._connection import get_db
        code = db.generate_invite_code(admin_user)
        conn = get_db()
        row = conn.execute("SELECT id FROM invite_codes WHERE code=?", (code,)).fetchone()
        code_id = row["id"]
        conn.close()

        db.delete_invite_code(code_id)
        expired = db.list_expired_codes()
        expired_codes = [r["code"] for r in expired]
        assert code in expired_codes

    def test_code_format_matches_xxxx_xxxx(self, admin_user):
        import db
        import re
        code = db.generate_invite_code(admin_user)
        assert re.match(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$", code), f"Unexpected code format: {code}"

    def test_use_invite_code_returns_false_when_already_used(self, admin_user, editor_user):
        import db
        from werkzeug.security import generate_password_hash
        code = db.generate_invite_code(admin_user)
        uid2 = db.create_user("user2", generate_password_hash("pw"), role="user")
        db.use_invite_code(code, editor_user)
        result = db.use_invite_code(code, uid2)
        assert result is False
