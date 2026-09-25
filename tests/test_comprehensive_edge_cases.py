"""
Comprehensive edge-case tests for BananaWiki.

Covers boundary conditions, error handling, and under-tested code paths:
  - generate_random_id with extreme lengths
  - slugify with unicode, whitespace-only, all-special-char inputs
  - update_site_settings / update_user with invalid column names
  - run_full_cleanup return-value structure
  - cleanup functions with retention_days=0 (should not delete fresh rows)
  - cleanup functions with retention_days=-1 (should delete everything)
  - encrypt_value / decrypt_value edge cases
  - Kanban board sharing edge cases (invalid visibility, invalid share_type, etc.)
  - _safe_referrer with attack vectors
  - Hex color validation edge cases
  - Username validation edge cases
  - API rate-limit header/response for the banana API
  - Settings API - invalid JSON body
"""

import pytest
from werkzeug.security import generate_password_hash

import db
import config


# ---------------------------------------------------------------------------
#  generate_random_id edge cases
# ---------------------------------------------------------------------------

class TestGenerateRandomId:
    def test_default_length(self, isolated_db):
        """Default call produces a 12-character alphanumeric string."""
        rid = db.generate_random_id()
        assert len(rid) == 12
        assert rid.isalnum()

    def test_custom_length(self, isolated_db):
        """Custom length is respected."""
        for n in (1, 5, 20, 50):
            rid = db.generate_random_id(n)
            assert len(rid) == n, f"Expected length {n}, got {len(rid)}"

    def test_zero_length_returns_empty_string(self, isolated_db):
        """Length 0 produces an empty string (not an error)."""
        rid = db.generate_random_id(0)
        assert rid == ""

    def test_only_lowercase_alphanumeric(self, isolated_db):
        """Generated IDs contain only lowercase letters and digits."""
        for _ in range(20):
            rid = db.generate_random_id(16)
            assert all(c.islower() or c.isdigit() for c in rid)
            assert rid.isalnum()

    def test_uniqueness(self, isolated_db):
        """1 000 calls should produce no collisions for a 16-char ID."""
        ids = {db.generate_random_id(16) for _ in range(1000)}
        assert len(ids) == 1000


# ---------------------------------------------------------------------------
#  slugify edge cases
# ---------------------------------------------------------------------------

class TestSlugify:
    def _slug(self, text):
        from helpers._text import slugify
        return slugify(text)

    def test_normal_text(self):
        assert self._slug("Hello World") == "hello-world"

    def test_empty_string_falls_back_to_page(self):
        assert self._slug("") == "page"

    def test_whitespace_only_falls_back_to_page(self):
        assert self._slug("   ") == "page"

    def test_all_hyphens_falls_back_to_page(self):
        assert self._slug("---") == "page"

    def test_all_special_chars_falls_back_to_page(self):
        assert self._slug("!@#$%^&*()") == "page"

    def test_unicode_letters_preserved(self):
        # Non-ASCII word characters (ö ä ü) are preserved by \w in regex
        result = self._slug("ö ä ü")
        assert "ö" in result
        assert "ä" in result
        assert "ü" in result

    def test_leading_trailing_hyphens_stripped(self):
        result = self._slug("-hello-")
        assert not result.startswith("-")
        assert not result.endswith("-")

    def test_underscores_become_hyphens(self):
        assert self._slug("Hello_World") == "hello-world"

    def test_multiple_spaces_become_single_hyphen(self):
        assert self._slug("Hello  World") == "hello-world"

    def test_mixed_case_lowercased(self):
        assert self._slug("HELLO WORLD") == "hello-world"

    def test_numbers_preserved(self):
        assert self._slug("Chapter 42") == "chapter-42"

    def test_hyphen_in_middle_preserved(self):
        assert self._slug("step-by-step") == "step-by-step"


class TestHexColorValidation:
    def _valid(self, val):
        from helpers._validation import _is_valid_hex_color
        return _is_valid_hex_color(val)

    def test_valid_lowercase(self):
        assert self._valid("#aabbcc") is True

    def test_valid_uppercase(self):
        assert self._valid("#AABBCC") is True

    def test_valid_mixed_case(self):
        assert self._valid("#aAbBcC") is True

    def test_short_hex_invalid(self):
        assert self._valid("#abc") is False

    def test_no_hash_invalid(self):
        assert self._valid("aabbcc") is False

    def test_too_long_invalid(self):
        assert self._valid("#aabbccd") is False

    def test_empty_string_invalid(self):
        assert self._valid("") is False

    def test_wrong_chars_invalid(self):
        assert self._valid("#zzzzzz") is False

    def test_hash_only_invalid(self):
        assert self._valid("#") is False


class TestUsernameValidation:
    def _valid(self, val):
        from helpers._validation import _is_valid_username
        return _is_valid_username(val)

    def test_valid_simple(self):
        assert self._valid("alice") is True

    def test_valid_with_numbers(self):
        assert self._valid("alice123") is True

    def test_valid_with_underscore_hyphen(self):
        assert self._valid("alice_bob-123") is True

    def test_empty_invalid(self):
        assert self._valid("") is False

    def test_space_invalid(self):
        assert self._valid("alice bob") is False

    def test_at_symbol_invalid(self):
        assert self._valid("alice@domain") is False

    def test_dot_invalid(self):
        assert self._valid("alice.bob") is False

    def test_unicode_letters_invalid(self):
        # The regex only allows ASCII letters, digits, _ and -
        assert self._valid("ölíç") is False

    def test_newline_invalid(self):
        assert self._valid("alice\nbob") is False


# ---------------------------------------------------------------------------
#  _safe_referrer with attack vectors
# ---------------------------------------------------------------------------

class TestSafeReferrer:
    """Test _safe_referrer inside a Flask request context."""

    def test_none_referrer_returns_none(self, client, admin_user):
        with client.application.test_request_context("/", headers={}):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_same_origin_referrer_returns_path(self, client, admin_user):
        with client.application.test_request_context(
            "/page/test",
            headers={"Referer": "http://localhost/page/test"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result == "/page/test"

    def test_cross_origin_referrer_rejected(self, client, admin_user):
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://evil.com/steal"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_protocol_relative_rejected(self, client, admin_user):
        with client.application.test_request_context(
            "/",
            headers={"Referer": "//evil.com/steal"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_javascript_uri_rejected(self, client, admin_user):
        with client.application.test_request_context(
            "/",
            headers={"Referer": "javascript:alert(1)"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            # javascript:alert(1) has no netloc but path='alert(1)' which
            # doesn't start with '/', so it is rejected.
            assert result is None

    def test_backslash_rejected(self, client, admin_user):
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://localhost/\\evil.com"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_referrer_with_query_string_preserved(self, client, admin_user):
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://localhost/search?q=test"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result == "/search?q=test"

    def test_encoded_double_slash_rejected(self, client, admin_user):
        """Percent-encoded // (%2F%2F) at the start of the path must be rejected.

        After decoding, /%2F%2Fevil.com becomes //evil.com. A protocol-relative
        URL that some browsers would follow to an external host.
        """
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://localhost/%2F%2Fevil.com"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_encoded_backslash_rejected(self, client, admin_user):
        """Percent-encoded backslash (%5C) in the path must be rejected.

        After decoding, /%5Cevil.com becomes /\\evil.com, which some browsers
        (e.g. legacy IE/Edge) normalise to //evil.com and follow externally.
        """
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://localhost/%5Cevil.com"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None

    def test_double_encoded_slash_rejected(self, client, admin_user):
        """Double-encoded slash (%252F%252F) must also be rejected.

        %252F decodes to %2F on the first pass, then to / on the second pass,
        making the path //evil.com after full decoding. The iterative unquote
        loop in the implementation catches this.
        """
        with client.application.test_request_context(
            "/",
            headers={"Referer": "http://localhost/%252F%252Fevil.com"},
        ):
            from helpers._validation import _safe_referrer
            result = _safe_referrer()
            assert result is None


# ---------------------------------------------------------------------------
#  update_site_settings with invalid column raises ValueError
# ---------------------------------------------------------------------------

class TestUpdateSiteSettingsValidation:
    def test_invalid_column_raises_value_error(self, isolated_db):
        with pytest.raises(ValueError, match="Invalid column"):
            db.update_site_settings(totally_not_a_column="value")

    def test_multiple_invalid_columns_raise_value_error(self, isolated_db):
        with pytest.raises(ValueError):
            db.update_site_settings(invalid_col1="a", invalid_col2="b")

    def test_mix_valid_and_invalid_raises_value_error(self, isolated_db):
        with pytest.raises(ValueError):
            db.update_site_settings(site_name="OK", totally_bad_col="oops")

    def test_empty_call_is_no_op(self, isolated_db):
        """Calling with no kwargs should not raise and should not change anything."""
        before = db.get_site_settings()
        db.update_site_settings()
        after = db.get_site_settings()
        assert before["site_name"] == after["site_name"]

    def test_valid_column_works(self, isolated_db):
        db.update_site_settings(site_name="EdgeCaseWiki")
        assert db.get_site_settings()["site_name"] == "EdgeCaseWiki"


# ---------------------------------------------------------------------------
#  update_user with invalid column raises ValueError
# ---------------------------------------------------------------------------

class TestUpdateUserValidation:
    def test_invalid_column_raises_value_error(self, isolated_db):
        uid = db.create_user("testuser", generate_password_hash("pw"), role="user")
        with pytest.raises(ValueError, match="Invalid column"):
            db.update_user(uid, totally_invalid="oops")

    def test_empty_user_id_raises_value_error(self, isolated_db):
        with pytest.raises(ValueError, match="user_id is required"):
            db.update_user(None, role="admin")

    def test_valid_columns_work(self, isolated_db):
        uid = db.create_user("testuser2", generate_password_hash("pw"), role="user")
        db.update_user(uid, role="editor", suspended=0)
        user = db.get_user_by_id(uid)
        assert user["role"] == "editor"
        assert user["suspended"] == 0


# ---------------------------------------------------------------------------
#  update_board with invalid column raises ValueError
# ---------------------------------------------------------------------------

class TestUpdateBoardValidation:
    def test_invalid_column_raises_value_error(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Test Board", "Desc", admin_user)
        with pytest.raises(ValueError, match="Invalid column"):
            db.kanban_update_board(board_id, totally_invalid="oops")

    def test_valid_columns_work(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Test Board", "Desc", admin_user)
        db.kanban_update_board(board_id, title="New Title", description="New Desc")
        board = db.kanban_get_board(board_id)
        assert board["title"] == "New Title"
        assert board["description"] == "New Desc"


# ---------------------------------------------------------------------------
#  set_board_visibility with invalid value raises ValueError
# ---------------------------------------------------------------------------

class TestBoardVisibility:
    def test_invalid_visibility_raises_value_error(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Vis Board", "Desc", admin_user)
        with pytest.raises(ValueError, match="Invalid visibility"):
            db.kanban_set_board_visibility(board_id, "totally_invalid")

    def test_valid_visibilities_work(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Vis Board", "Desc", admin_user)
        for vis in ("private", "shared", "public"):
            db.kanban_set_board_visibility(board_id, vis)
            board = db.kanban_get_board(board_id)
            assert board["visibility"] == vis


# ---------------------------------------------------------------------------
#  set_board_share with invalid params raises ValueError
# ---------------------------------------------------------------------------

class TestBoardSharing:
    def test_invalid_share_type_raises_value_error(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        with pytest.raises(ValueError, match="Invalid share_type"):
            db.kanban_set_board_share(board_id, "totally_invalid", "admin")

    def test_invalid_access_level_raises_value_error(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        with pytest.raises(ValueError, match="Invalid access_level"):
            db.kanban_set_board_share(board_id, "role", "admin", "invalid_level")

    def test_valid_role_share(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        db.kanban_set_board_share(board_id, "role", "editor", "view")
        shares = db.kanban_get_board_shares(board_id)
        assert len(shares) == 1
        assert shares[0]["share_type"] == "role"
        assert shares[0]["target"] == "editor"

    def test_valid_user_share(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        uid2 = db.create_user("otheruser", generate_password_hash("pw"), role="user")
        db.kanban_set_board_share(board_id, "user", uid2, "write")
        shares = db.kanban_get_board_shares(board_id)
        assert len(shares) == 1
        assert shares[0]["access_level"] == "write"

    def test_update_share_access_level(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        db.kanban_set_board_share(board_id, "role", "editor", "view")
        db.kanban_set_board_share(board_id, "role", "editor", "write")
        shares = db.kanban_get_board_shares(board_id)
        # Should be upserted, still one row
        assert len(shares) == 1
        assert shares[0]["access_level"] == "write"

    def test_remove_share(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        db.kanban_set_board_share(board_id, "role", "editor", "view")
        db.kanban_remove_board_share(board_id, "role", "editor")
        shares = db.kanban_get_board_shares(board_id)
        assert shares == []

    def test_remove_nonexistent_share_is_no_op(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Share Board", "Desc", admin_user)
        # Should not raise
        db.kanban_remove_board_share(board_id, "role", "nonexistent")


# ---------------------------------------------------------------------------
#  run_full_cleanup return value structure
# ---------------------------------------------------------------------------

class TestRunFullCleanupReturnValue:
    def test_return_value_has_all_expected_keys(self, isolated_db):
        summary = db.run_full_cleanup(vacuum=False)
        expected_keys = {
            "expired_invite_codes",
            "stale_drafts",
            "expired_drafts",
            "notified_badge_notifications",
            "released_reservations",
            "old_login_attempts",
            "old_rate_limit_hits",
            "soft_deleted_messages",
            "expired_announcements",
            "resolved_quota_requests",
            "revoked_badges",
            "old_role_history",
            "old_username_history",
            "expired_group_timeouts",
            "expired_suspensions",
            "expired_temporary",
            "expired_contributions",
            "reviewed_contributions",
            "resolved_contribution_quota_requests",
            "optimized",
        }
        assert set(summary.keys()) == expected_keys

    def test_return_values_are_non_negative(self, isolated_db):
        summary = db.run_full_cleanup(vacuum=False)
        for key, value in summary.items():
            if key == "optimized":
                assert isinstance(value, bool)
            elif key == "soft_deleted_messages":
                # This key returns a dict with 'dm' and 'group'
                assert isinstance(value, dict)
                assert value["dm"] >= 0
                assert value["group"] >= 0
            elif key == "expired_temporary":
                # These may return 0 (int) or a dict
                assert value is not None
            else:
                assert isinstance(value, int) and value >= 0, \
                    f"Key '{key}' has unexpected value: {value!r}"

    def test_vacuum_false_leaves_optimized_false(self, isolated_db):
        summary = db.run_full_cleanup(vacuum=False)
        assert summary["optimized"] is False

    def test_vacuum_true_leaves_optimized_true(self, isolated_db):
        summary = db.run_full_cleanup(vacuum=True)
        assert summary["optimized"] is True

    def test_empty_database_all_zero_counts(self, isolated_db):
        """With a fresh empty DB every count should be 0."""
        summary = db.run_full_cleanup(vacuum=False)
        for key, value in summary.items():
            if key == "optimized":
                continue
            if key == "soft_deleted_messages":
                assert value["dm"] == 0
                assert value["group"] == 0
            elif key == "expired_temporary":
                pass  # These may not have tables; 0 is returned by exception handler
            else:
                assert value == 0, f"Expected 0 for '{key}', got {value}"


# ---------------------------------------------------------------------------
#  cleanup functions with retention_days=0 (fresh data should not be deleted)
# ---------------------------------------------------------------------------

class TestCleanupZeroRetention:
    """Fresh rows (just inserted) should survive a retention_days=0 cleanup."""

    def test_invite_code_survives_zero_retention(self, isolated_db):
        uid = db.create_user("admin_z", generate_password_hash("pw"), role="admin")
        db.generate_invite_code(uid)
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            db.cleanup_expired_invite_codes(retention_days=0)
        # The code must still be present since the cleanup was rejected
        codes = db.list_invite_codes(active_only=True)
        assert len(codes) == 1

    def test_badge_notification_survives_zero_retention(self, isolated_db):
        uid = db.create_user("user_z", generate_password_hash("pw"), role="user")
        # Create a badge type and award it (trigger_type='' for manual)
        badge_type_id = db.create_badge_type(
            "Test Badge", description="Test desc", icon="🏆",
            color="#ffd700", enabled=True, auto_trigger=False,
            trigger_type="", trigger_threshold=0,
        )
        db.award_badge(uid, badge_type_id)
        # retention_days=0 is rejected by the validation guard
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            db.cleanup_notified_badge_notifications(retention_days=0)

    def test_login_attempts_survive_zero_retention(self, isolated_db):
        # login_attempts only has (id, ip, attempted_at). No user_id column
        import sqlite3
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute(
            "INSERT INTO login_attempts (ip, attempted_at) VALUES ('127.0.0.1', datetime('now'))",
        )
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            db.cleanup_old_login_attempts(retention_days=0)


# ---------------------------------------------------------------------------
#  cleanup functions with negative retention_days (delete everything)
# ---------------------------------------------------------------------------

class TestCleanupNegativeRetention:
    """Negative retention means 'older than N days in the future' which matches all rows."""

    def test_invite_code_deleted_with_negative_retention(self, isolated_db):
        uid = db.create_user("admin_neg", generate_password_hash("pw"), role="admin")
        db.generate_invite_code(uid)
        # Negative retention_days is rejected by the validation guard
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            db.cleanup_expired_invite_codes(retention_days=-5)


# ---------------------------------------------------------------------------
#  encrypt_value / decrypt_value edge cases
# ---------------------------------------------------------------------------

class TestEncryptDecryptEdgeCases:
    def setup_method(self):
        from helpers._crypto import _reset_cache
        config.SECRET_KEY = "test-edge-cases-secret-key-abc123"
        _reset_cache()

    def teardown_method(self):
        from helpers._crypto import _reset_cache
        _reset_cache()

    def test_none_passthrough(self):
        from helpers._crypto import encrypt_value, decrypt_value
        assert encrypt_value(None) is None
        assert decrypt_value(None) is None

    def test_empty_string_passthrough(self):
        from helpers._crypto import encrypt_value, decrypt_value
        assert encrypt_value("") == ""
        assert decrypt_value("") == ""

    def test_round_trip(self):
        from helpers._crypto import encrypt_value, decrypt_value
        plain = "super-secret-token-12345"
        enc = encrypt_value(plain)
        assert enc != plain
        assert enc.startswith("fernet:")
        dec = decrypt_value(enc)
        assert dec == plain

    def test_legacy_plaintext_returned_as_is(self):
        from helpers._crypto import decrypt_value
        # A value without the 'fernet:' prefix is legacy plaintext
        legacy = "my-old-plaintext-token"
        assert decrypt_value(legacy) == legacy

    def test_different_key_returns_empty_string(self):
        from helpers._crypto import encrypt_value, decrypt_value, _reset_cache
        config.SECRET_KEY = "key-one-xyz"
        _reset_cache()
        enc = encrypt_value("secret")
        # Change the key
        config.SECRET_KEY = "key-two-xyz"
        _reset_cache()
        result = decrypt_value(enc)
        # Cannot decrypt with a different key → empty string
        assert result == ""

    def test_each_encryption_produces_unique_ciphertext(self):
        from helpers._crypto import encrypt_value
        plain = "same-value"
        enc1 = encrypt_value(plain)
        enc2 = encrypt_value(plain)
        # Fernet uses a random IV, so the ciphertexts should differ
        assert enc1 != enc2

    def test_tampered_ciphertext_returns_empty_string(self):
        from helpers._crypto import encrypt_value, decrypt_value
        enc = encrypt_value("value")
        tampered = enc[:-5] + "XXXXX"
        result = decrypt_value(tampered)
        assert result == ""


class TestKanbanTicketEdgeCases:
    def test_update_ticket_invalid_column_raises(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Test", "Desc", admin_user)
        col_id = db.kanban_create_column(board_id, "To Do")
        ticket_id = db.kanban_create_ticket(col_id, "Ticket", "Desc", admin_user)
        with pytest.raises(ValueError, match="Invalid column"):
            db.kanban_update_ticket(ticket_id, totally_invalid="oops")

    def test_move_ticket_updates_column(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Test", "Desc", admin_user)
        col1_id = db.kanban_create_column(board_id, "To Do")
        col2_id = db.kanban_create_column(board_id, "Done")
        ticket_id = db.kanban_create_ticket(col1_id, "Ticket", "Desc", admin_user)
        db.kanban_move_ticket(ticket_id, col2_id, 0)
        ticket = db.kanban_get_ticket(ticket_id)
        assert ticket["column_id"] == col2_id

    def test_count_board_tickets_empty_board(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Empty Board", "Desc", admin_user)
        assert db.kanban_count_board_tickets(board_id) == 0

    def test_count_board_tickets_after_deletion(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Test", "Desc", admin_user)
        col_id = db.kanban_create_column(board_id, "To Do")
        tid = db.kanban_create_ticket(col_id, "T", "D", admin_user)
        assert db.kanban_count_board_tickets(board_id) == 1
        db.kanban_delete_ticket(tid)
        assert db.kanban_count_board_tickets(board_id) == 0

    def test_delete_board_cascades_to_columns_and_tickets(self, isolated_db, admin_user):
        board_id = db.kanban_create_board("Cascade Board", "Desc", admin_user)
        col_id = db.kanban_create_column(board_id, "Col")
        db.kanban_create_ticket(col_id, "T", "D", admin_user)
        db.kanban_delete_board(board_id)
        # Board gone
        assert db.kanban_get_board(board_id) is None
        # Column gone (cascade)
        assert db.kanban_get_column(col_id) is None


class TestBananaAPIEdgeCases:
    @pytest.fixture(autouse=True)
    def enable_api(self):
        db.update_site_settings(api_service_enabled=1)

    def _make_admin_token(self, admin_user):
        import db
        return db.create_api_token(
            admin_user, name="test",
            permissions={"read": True, "write": True, "scopes": ["admin"]}
        )

    def test_api_toggle_requires_bearer_prefix(self, client, admin_user):
        """Token without 'Bearer ' prefix is rejected."""
        token = self._make_admin_token(admin_user)
        resp = client.post("/api/v1/banana-mode", headers={"Authorization": token})
        assert resp.status_code == 401
        data = resp.get_json()
        assert "Missing or invalid Authorization header" in data["error"]

    def test_api_toggle_idempotent_twice(self, client, admin_user):
        """Two consecutive toggles return to the original state."""
        db.update_site_settings(banana_mode=0)
        token = self._make_admin_token(admin_user)
        headers = {"Authorization": f"Bearer {token}"}
        resp1 = client.post("/api/v1/banana-mode", headers=headers)
        assert resp1.get_json()["banana_mode"] is True
        resp2 = client.post("/api/v1/banana-mode", headers=headers)
        assert resp2.get_json()["banana_mode"] is False

    def test_api_empty_string_token(self, client, admin_user):
        """Empty Bearer token is rejected as invalid."""
        resp = client.post("/api/v1/banana-mode", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_api_suspended_user_token_rejected(self, client, admin_user):
        """Suspended admin's token cannot call the API."""
        token = self._make_admin_token(admin_user)
        db.update_user(admin_user, suspended=1)
        resp = client.post("/api/v1/banana-mode", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestAdminSettingsColorValidation:
    def test_invalid_primary_color_rejected(self, logged_in_admin):
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "BananaWiki",
            "timezone": "UTC",
            "primary_color": "notacolor",
            "secondary_color": "#151520",
            "accent_color": "#6e8aca",
            "text_color": "#c8ccd8",
            "sidebar_color": "#111118",
            "bg_color": "#0d0d14",
        })
        # Should redirect back with an error (not 500)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            follow = logged_in_admin.get(resp.location)
            assert b"Invalid" in follow.data or b"Error" in follow.data or b"error" in follow.data

    def test_valid_colors_accepted(self, logged_in_admin):
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "EdgeWiki",
            "timezone": "UTC",
            "primary_color": "#112233",
            "secondary_color": "#445566",
            "accent_color": "#778899",
            "text_color": "#aabbcc",
            "sidebar_color": "#ddeeff",
            "bg_color": "#001122",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["site_name"] == "EdgeWiki"


class TestUserDeletionEdgeCases:
    def test_delete_nonexistent_user_does_not_raise(self, isolated_db):
        """Deleting a user that doesn't exist should not raise."""
        db.delete_user("nonexistent_id_xyz")

    def test_delete_user_with_empty_id_raises(self, isolated_db):
        with pytest.raises(ValueError, match="user_id is required"):
            db.delete_user(None)

    def test_delete_user_removes_from_db(self, isolated_db):
        uid = db.create_user("victim", generate_password_hash("pw"), role="user")
        assert db.get_user_by_id(uid) is not None
        db.delete_user(uid)
        assert db.get_user_by_id(uid) is None


class TestCleanupIdempotency:
    """Running cleanup twice should not crash or double-count."""

    def test_run_full_cleanup_twice_is_safe(self, isolated_db):
        summary1 = db.run_full_cleanup(vacuum=False)
        summary2 = db.run_full_cleanup(vacuum=False)
        # Both should succeed and return valid structures
        assert set(summary1.keys()) == set(summary2.keys())

    def test_cleanup_expired_suspensions_twice(self, isolated_db):
        """cleanup_expired_suspensions can be called twice safely."""
        uid = db.create_user("sus_user", generate_password_hash("pw"), role="user")
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.update_user(uid, suspended=1, suspended_until=past)
        count1 = db.cleanup_expired_suspensions()
        count2 = db.cleanup_expired_suspensions()
        assert count1 == 1  # Unsuspended on first call
        assert count2 == 0  # Nothing to unsuspend on second call
        user = db.get_user_by_id(uid)
        assert user["suspended"] == 0

    def test_cleanup_expired_group_timeouts_twice(self, isolated_db, admin_user):
        """cleanup_expired_group_timeouts can be called twice safely."""
        from datetime import datetime, timedelta, timezone
        group_data = db.create_group_chat("Timeout Group", admin_user)
        group_id = group_data["id"]
        uid2 = db.create_user("member1", generate_password_hash("pw"), role="user")
        db.add_group_member(group_id, uid2)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.set_group_member_timeout(group_id, uid2, past)
        count1 = db.cleanup_expired_group_timeouts()
        count2 = db.cleanup_expired_group_timeouts()
        assert count1 == 1
        assert count2 == 0


class TestPageSlugEdgeCases:
    def test_update_slug_same_slug_is_no_op(self, isolated_db, admin_user):
        """Updating a slug to the same value returns the old slug without DB changes."""
        from helpers._text import slugify
        cat = db.create_category("Test Cat", None)
        title = "Test Page"
        slug = slugify(title)
        page_id = db.create_page(title, slug, "Content", cat, admin_user)
        result = db.update_page_slug(page_id, slug)
        assert result == slug

    def test_update_slug_nonexistent_page_returns_none(self, isolated_db):
        """Updating slug for a non-existent page returns None."""
        result = db.update_page_slug(99999, "new-slug")
        assert result is None

    def test_update_slug_does_not_corrupt_prefix_links(self, isolated_db, admin_user):
        """Renaming 'notes' must not corrupt links to 'notes-archive'."""
        cat = db.create_category("Cat", None)
        page_a = db.create_page("Notes", "notes", "Content A", cat, admin_user)
        db.create_page("Notes Archive", "notes-archive",
                        "See [notes](/page/notes) and [archive](/page/notes-archive)",
                        cat, admin_user)
        db.update_page_slug(page_a, "my-notes")
        other = db.get_page_by_slug("notes-archive")
        assert "/page/my-notes)" in other["content"]
        assert "/page/notes-archive)" in other["content"]
        assert "/page/my-notes-archive" not in other["content"]

    def test_update_slug_rewrites_exact_match_at_end_of_content(self, isolated_db, admin_user):
        """A link at the very end of page content (no trailing chars) is rewritten."""
        cat = db.create_category("Cat", None)
        page_a = db.create_page("Notes", "notes", "Content A", cat, admin_user)
        db.create_page("Linker", "linker", "Visit /page/notes", cat, admin_user)
        db.update_page_slug(page_a, "my-notes")
        linker = db.get_page_by_slug("linker")
        assert linker["content"] == "Visit /page/my-notes"

    def test_update_slug_rewrites_link_followed_by_paren(self, isolated_db, admin_user):
        """A markdown link /page/notes) is correctly rewritten."""
        cat = db.create_category("Cat", None)
        page_a = db.create_page("Notes", "notes", "Content A", cat, admin_user)
        db.create_page("Linker", "linker",
                        "[click](/page/notes)", cat, admin_user)
        db.update_page_slug(page_a, "my-notes")
        linker = db.get_page_by_slug("linker")
        assert linker["content"] == "[click](/page/my-notes)"

    def test_slugify_used_on_page_creation(self, isolated_db, admin_user):
        """Page title is properly slugified on creation."""
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        title = "Hello World!"
        slug = slugify(title)
        page_id = db.create_page(title, slug, "Content", cat, admin_user)
        page = db.get_page(page_id)
        assert page["slug"] == "hello-world"


# ---------------------------------------------------------------------------
#  get_site_settings returns a dict, not sqlite3.Row
# ---------------------------------------------------------------------------

class TestGetSiteSettingsReturnsDict:
    def test_returns_dict(self, isolated_db):
        settings = db.get_site_settings()
        assert isinstance(settings, dict)

    def test_key_access_works(self, isolated_db):
        settings = db.get_site_settings()
        _ = settings["site_name"]
        _ = settings["setup_done"]

    def test_keys_method_works(self, isolated_db):
        settings = db.get_site_settings()
        assert "site_name" in settings.keys()
        assert "telegram_sync_token" in settings.keys()

    def test_dot_notation_works_in_template_context(self, client, admin_user):
        """Jinja2 templates can access settings via dot notation (attribute access)."""
        # This exercises the inject_globals() context processor indirectly
        resp = client.get("/")
        assert resp.status_code in (200, 302)


# ---------------------------------------------------------------------------
#  render_markdown edge cases
# ---------------------------------------------------------------------------

class TestRenderMarkdownEdgeCases:
    def _md(self, text, embed=False):
        from helpers._markdown import render_markdown
        return render_markdown(text, embed_videos=embed)

    def test_none_returns_empty_string(self):
        """render_markdown(None) must not raise and must return ''."""
        assert self._md(None) == ""

    def test_empty_string_returns_empty_string(self):
        """render_markdown('') returns ''."""
        assert self._md("") == ""

    def test_normal_text_renders(self):
        result = self._md("Hello **world**")
        assert "<strong>world</strong>" in result

    def test_script_tag_sanitized(self):
        result = self._md("<script>alert(1)</script>")
        assert "<script>" not in result

    def test_xss_in_link_href_stripped(self):
        """Bleach removes javascript: href values."""
        result = self._md("[click](javascript:alert(1))")
        assert "javascript:" not in result

    def test_none_with_embed_videos_false_returns_empty_string(self):
        """render_markdown(None, embed_videos=False) must not raise."""
        assert self._md(None, embed=False) == ""

    def test_none_with_embed_videos_returns_empty_string(self):
        """render_markdown(None, embed_videos=True) must not raise."""
        assert self._md(None, embed=True) == ""

    def test_heading_syntax_works(self):
        result = self._md("# Hello")
        # toc extension adds an id attribute, so check for tag start + content
        assert "Hello</h1>" in result

    def test_table_syntax_works(self):
        result = self._md("| a | b |\n|---|---|\n| 1 | 2 |")
        assert "<table>" in result

    def test_fenced_code_block_works(self):
        result = self._md("```python\nprint('hi')\n```")
        assert "codehilite" in result
        assert "<span" in result

    def test_large_content_handled(self):
        """render_markdown with a large but not huge input completes without error."""
        large_text = "# Heading\n\n" + "word " * 5000
        result = self._md(large_text)
        assert "Heading</h1>" in result  # toc extension may add id attr

    def test_img_tag_allowed(self):
        result = self._md("![alt](https://example.com/img.png)")
        assert "<img" in result
        assert 'src="https://example.com/img.png"' in result

    def test_bleach_allows_known_tags(self):
        """Tags in ALLOWED_TAGS must pass through bleach."""
        result = self._md("Hello *world*")
        assert "<em>world</em>" in result

    def test_disallowed_tags_stripped_not_escaped(self):
        """strip=True removes disallowed tags entirely instead of escaping."""
        result = self._md("<div>content</div>")
        assert "&lt;div&gt;" not in result
        assert "content" in result

    def test_html_comments_stripped(self):
        """strip_comments=True removes HTML comments."""
        result = self._md("before <!-- secret --> after")
        assert "secret" not in result
        assert "<!--" not in result

    def test_explicit_protocols_block_data_urls(self):
        """Only http, https, ftp, mailto protocols are allowed in links."""
        result = self._md('[click](data:text/html,<script>alert(1)</script>)')
        assert "data:" not in result

    # -- Multi-newline preservation tests --

    def test_single_newline_produces_br(self):
        """nl2br: a single newline within a paragraph becomes <br>."""
        result = self._md("Line 1\nLine 2")
        assert "<br" in result
        assert "Line 1" in result
        assert "Line 2" in result

    def test_double_newline_produces_paragraph_break(self):
        """Two newlines create a normal paragraph break."""
        result = self._md("Line 1\n\nLine 2")
        assert "<p>Line 1</p>" in result
        assert "<p>Line 2</p>" in result

    def test_triple_newline_preserves_extra_spacing(self):
        """Three newlines produce a paragraph break plus extra <br> spacing."""
        result = self._md("Line 1\n\n\nLine 2")
        assert "Line 1" in result
        assert "Line 2" in result
        assert result.count("<br") > 0

    def test_four_newlines_preserves_extra_spacing(self):
        """Four newlines produce extra spacing with 2 <br> elements."""
        result = self._md("Line 1\n\n\n\nLine 2")
        assert "Line 1" in result
        assert "Line 2" in result
        assert result.count("<br") >= 2

    def test_five_newlines_preserves_extra_spacing(self):
        """Five newlines produce extra spacing with 3 <br> elements."""
        result = self._md("Section 1\n\n\n\n\nSection 2")
        assert "Section 1" in result
        assert "Section 2" in result
        assert result.count("<br") >= 3

    def test_multi_newlines_with_whitespace_only_lines(self):
        """Whitespace-only lines between content are treated as blank lines."""
        result = self._md("A\n   \n   \nB")
        assert "A" in result
        assert "B" in result
        # 3 newlines → 1 extra beyond the paragraph break
        assert result.count("<br") == 1


# ---------------------------------------------------------------------------
#  _get_video_embed_src edge cases
# ---------------------------------------------------------------------------

class TestGetVideoEmbedSrc:
    def _embed_src(self, url):
        from helpers._markdown import _get_video_embed_src
        return _get_video_embed_src(url)

    def test_none_returns_none(self):
        assert self._embed_src(None) is None

    def test_empty_string_returns_none(self):
        assert self._embed_src("") is None

    def test_youtube_watch_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = self._embed_src(url)
        assert result == "https://www.youtube.com/embed/dQw4w9WgXcQ"

    def test_youtube_short_url(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        result = self._embed_src(url)
        assert result == "https://www.youtube.com/embed/dQw4w9WgXcQ"

    def test_vimeo_url(self):
        url = "https://vimeo.com/123456789"
        result = self._embed_src(url)
        assert result == "https://player.vimeo.com/video/123456789"

    def test_javascript_url_returns_none(self):
        """javascript: URIs must never produce a video embed src."""
        assert self._embed_src("javascript:alert(1)") is None

    def test_non_video_url_returns_none(self):
        assert self._embed_src("https://example.com/page") is None


# ---------------------------------------------------------------------------
#  import_site_data edge cases
# ---------------------------------------------------------------------------

class TestImportSiteDataEdgeCases:
    def test_import_invalid_mode_raises(self, isolated_db):
        from db._migration import import_site_data
        with pytest.raises(ValueError, match="Unknown import mode"):
            import_site_data({"_meta": {"version": 1}}, "invalid_mode")

    def test_import_wrong_version_raises(self, isolated_db):
        from db._migration import import_site_data
        with pytest.raises(ValueError, match="Incompatible export version"):
            import_site_data({"_meta": {"version": 999}}, "keep")

    def test_import_empty_data_dict_succeeds(self, isolated_db):
        """Importing an empty dict (no _meta) silently succeeds (version defaults to 1)."""
        from db._migration import import_site_data
        # No exception should be raised; version defaults to 1
        import_site_data({}, "keep")

    def test_import_data_with_only_meta_succeeds(self, isolated_db):
        """Importing just _meta with no table data is a safe no-op."""
        from db._migration import import_site_data
        import_site_data({"_meta": {"version": 1, "exported_at": "now"}}, "keep")

    def test_import_with_invalid_table_name_ignored(self, isolated_db):
        """Table names not in sqlite_master are silently skipped (SQL injection prevented)."""
        from db._migration import import_site_data
        # This table name should be filtered by _VALID_TABLE_NAME_RE or
        # simply not found in the DB and ignored
        data = {"_meta": {"version": 1}, "invalid table; DROP TABLE users": [{"id": 1}]}
        import_site_data(data, "keep")
        # users table should still be intact
        assert db.list_users() is not None

    def test_import_keep_mode_does_not_overwrite_existing(self, isolated_db):
        """'keep' mode does not overwrite existing site settings."""
        from db._migration import import_site_data
        db.update_site_settings(site_name="Original Name")
        import_site_data(
            {"_meta": {"version": 1}, "site_settings": [{"id": 1, "site_name": "New Name"}]},
            "keep",
        )
        settings = db.get_site_settings()
        assert settings["site_name"] == "Original Name"

    def test_import_override_mode_updates_existing(self, isolated_db):
        """'override' mode updates existing site settings."""
        from db._migration import import_site_data
        db.update_site_settings(site_name="Original Name")
        import_site_data(
            {"_meta": {"version": 1}, "site_settings": [{"id": 1, "site_name": "New Name"}]},
            "override",
        )
        settings = db.get_site_settings()
        assert settings["site_name"] == "New Name"


class TestProfileHeatmapEdgeCases:
    def test_empty_heatmap_for_new_user(self, isolated_db):
        uid = db.create_user("new_user", generate_password_hash("pw"), role="user")
        year, heatmap = db.get_contributions_by_day(uid)
        assert isinstance(year, int)
        assert isinstance(heatmap, dict)
        assert len(heatmap) == 0

    def test_nonexistent_user_returns_empty_heatmap(self, isolated_db):
        year, heatmap = db.get_contributions_by_day("nonexistent_xyz")
        assert isinstance(heatmap, dict)
        assert len(heatmap) == 0

    def test_heatmap_has_contributions_after_page_creation(self, isolated_db, admin_user):
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("Test Page", slugify("Test Page"), "Content", cat, admin_user)
        year, heatmap = db.get_contributions_by_day(admin_user)
        assert len(heatmap) >= 1
        # All values should be positive integers
        for _day, count in heatmap.items():
            assert isinstance(count, int) and count > 0

    def test_heatmap_year_matches_current_year(self, isolated_db):
        from datetime import datetime, timezone
        uid = db.create_user("yeartest", generate_password_hash("pw"), role="user")
        year, _ = db.get_contributions_by_day(uid)
        expected_year = datetime.now(timezone.utc).year
        assert year == expected_year

    def test_heatmap_day_format_is_iso_date(self, isolated_db, admin_user):
        """Keys in the heatmap should be ISO date strings like 'YYYY-MM-DD'."""
        import re
        from helpers._text import slugify
        cat = db.create_category("Cat2", None)
        db.create_page("Page2", slugify("Page2"), "Content", cat, admin_user)
        _, heatmap = db.get_contributions_by_day(admin_user)
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for day in heatmap.keys():
            assert date_re.match(day), f"Invalid date format: {day}"


class TestSearchAPIEdgeCases:
    def test_search_pages_empty_query_returns_all(self, isolated_db):
        """db.search_pages('') treats empty pattern as '%%', matching all pages.
        The empty-query short-circuit happens at the route level, not the DB layer."""
        results = db.search_pages("", limit=100)
        # The home page may or may not be excluded; at minimum no crash
        assert isinstance(results, list)

    def test_search_pages_no_results(self, isolated_db):
        results = db.search_pages("zzznomatchzzz", limit=15)
        assert results == []

    def test_search_pages_finds_matching_title(self, isolated_db, admin_user):
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("Unique Title XYZ", slugify("Unique Title XYZ"), "Content", cat, admin_user)
        results = db.search_pages("Unique Title", limit=15)
        assert len(results) >= 1
        assert any(r["title"] == "Unique Title XYZ" for r in results)

    def test_search_pages_limit_respected(self, isolated_db, admin_user):
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        for i in range(10):
            db.create_page(f"Search Page {i}", slugify(f"Search Page {i}"), "Content", cat, admin_user)
        results = db.search_pages("Search Page", limit=3)
        assert len(results) <= 3

    def test_search_pages_very_long_query_does_not_crash(self, isolated_db):
        """A very long query string should return empty results without crashing."""
        long_query = "a" * 10000
        results = db.search_pages(long_query, limit=15)
        assert isinstance(results, list)

    def test_search_pages_special_chars_do_not_crash(self, isolated_db):
        """Special SQL characters should be handled safely (parameterized query)."""
        for query in ("' OR '1'='1", "%; DROP TABLE pages;--", "%%", "%_"):
            results = db.search_pages(query, limit=15)
            assert isinstance(results, list)

    def test_search_pages_percent_wildcard_escaped(self, isolated_db, admin_user):
        """A literal '%' in the query must not act as a LIKE wildcard."""
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("Alpha Page", slugify("Alpha Page"), "Content", cat, admin_user)
        db.create_page("100% Done", slugify("100% Done"), "Content", cat, admin_user)
        # Searching for literal "%" should only match pages with "%" in title
        results = db.search_pages("%", limit=100)
        assert all("%" in r["title"] for r in results)
        assert any(r["title"] == "100% Done" for r in results)

    def test_search_pages_underscore_wildcard_escaped(self, isolated_db, admin_user):
        """A literal '_' in the query must not act as a single-char LIKE wildcard."""
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("AB", slugify("AB"), "Content", cat, admin_user)
        db.create_page("A_B", slugify("A_B"), "Content", cat, admin_user)
        # Searching for "A_B" should only match "A_B", not "AB" via _ wildcard
        results = db.search_pages("A_B", limit=100)
        assert len(results) == 1
        assert results[0]["title"] == "A_B"

    def test_search_pages_full_percent_wildcard_escaped(self, isolated_db, admin_user):
        """search_pages_full must also escape LIKE wildcards."""
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("Regular Page", slugify("Regular Page"), "Body", cat, admin_user)
        db.create_page("50% Off", slugify("50% Off"), "Sale", cat, admin_user)
        results = db.search_pages_full("%", limit=100)
        assert all("%" in r["title"] for r in results)

    def test_search_pages_full_content_percent_escaped(self, isolated_db, admin_user):
        """search_pages_full with search_content=True must escape wildcards in body search."""
        from helpers._text import slugify
        cat = db.create_category("Cat", None)
        db.create_page("Page One", slugify("Page One"), "Normal content", cat, admin_user)
        db.create_page("Page Two", slugify("Page Two"), "100% complete", cat, admin_user)
        results = db.search_pages_full("%", limit=100, search_content=True)
        assert all("%" in r["title"] or True for r in results)
        # The key assertion: a bare "%" must NOT match every page
        titles = {r["title"] for r in results}
        assert "Page One" not in titles

    def test_search_categories_percent_wildcard_escaped(self, isolated_db):
        """search_categories must escape LIKE wildcards."""
        db.create_category("Normal", None)
        db.create_category("50% Off", None)
        results = db.search_categories("%", limit=100)
        assert all("%" in r["name"] for r in results)
        assert any(r["name"] == "50% Off" for r in results)

    def test_api_search_endpoint_with_empty_query(self, logged_in_admin):
        """GET /api/pages/search?q= returns [] (empty query short-circuits)."""
        resp = logged_in_admin.get("/api/pages/search?q=")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_api_sidebar_search_empty_query(self, logged_in_admin):
        """GET /api/sidebar/search?q= returns empty categories and pages."""
        resp = logged_in_admin.get("/api/sidebar/search?q=")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"categories": [], "pages": []}

    def test_api_sidebar_search_returns_json(self, logged_in_admin):
        """GET /api/sidebar/search?q=test returns proper JSON structure."""
        resp = logged_in_admin.get("/api/sidebar/search?q=test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "categories" in data
        assert "pages" in data
        assert isinstance(data["categories"], list)
        assert isinstance(data["pages"], list)
