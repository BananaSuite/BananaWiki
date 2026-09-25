"""Tests for the database field encryption feature (helpers/_crypto.py + db/_settings.py)."""

import sqlite3

import config


# ---------------------------------------------------------------------------
#  helpers._crypto unit tests
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    """Round-trip and edge-case tests for encrypt_value / decrypt_value."""

    def test_round_trip(self):
        from helpers._crypto import encrypt_value, decrypt_value
        plaintext = "123:BOTTOKEN"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext
        assert decrypt_value(encrypted) == plaintext

    def test_empty_string_passthrough(self):
        from helpers._crypto import encrypt_value, decrypt_value
        assert encrypt_value("") == ""
        assert decrypt_value("") == ""

    def test_none_passthrough(self):
        from helpers._crypto import encrypt_value, decrypt_value
        assert encrypt_value(None) is None
        assert decrypt_value(None) is None

    def test_encrypted_prefix(self):
        from helpers._crypto import encrypt_value, _ENCRYPTED_PREFIX
        encrypted = encrypt_value("secret")
        assert encrypted.startswith(_ENCRYPTED_PREFIX)

    def test_legacy_plaintext_returned_as_is(self):
        """Values without the fernet: prefix are treated as legacy plaintext."""
        from helpers._crypto import decrypt_value
        assert decrypt_value("old-token-value") == "old-token-value"

    def test_bad_ciphertext_returns_empty(self):
        """Corrupted ciphertext (with prefix) returns empty string."""
        from helpers._crypto import decrypt_value, _ENCRYPTED_PREFIX
        bad = _ENCRYPTED_PREFIX + "not-valid-fernet-data"
        assert decrypt_value(bad) == ""

    def test_different_key_returns_empty(self, monkeypatch):
        """Encrypted with one key, decrypted with another → empty string."""
        from helpers._crypto import encrypt_value, decrypt_value, _reset_cache

        encrypted = encrypt_value("my-secret")

        # Change the secret key so decryption uses a different derived key.
        monkeypatch.setattr(config, "SECRET_KEY", "completely-different-key")
        _reset_cache()

        assert decrypt_value(encrypted) == ""

    def test_each_encryption_is_unique(self):
        """Two encryptions of the same value should produce different ciphertexts."""
        from helpers._crypto import encrypt_value
        a = encrypt_value("same-value")
        b = encrypt_value("same-value")
        assert a != b  # Fernet uses a random IV each time


# ---------------------------------------------------------------------------
#  db._settings integration tests
# ---------------------------------------------------------------------------


class TestSettingsEncryption:
    """Verify that sensitive site_settings columns are encrypted at rest."""

    def test_non_sensitive_columns_not_encrypted(self):
        """Non-sensitive columns like site_name should remain plaintext."""
        import db
        db.update_site_settings(site_name="My Wiki")
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT site_name FROM site_settings WHERE id=1"
        ).fetchone()
        conn.close()
        assert row["site_name"] == "My Wiki"

    def test_settings_returned_as_dict(self):
        """get_site_settings() should return a dict with .keys() support."""
        import db
        settings = db.get_site_settings()
        assert isinstance(settings, dict)
        assert "site_name" in settings.keys()

    def test_gpu_tts_token_encrypted_in_db(self):
        """tts_gpu_auth_token should be encrypted in the raw database."""
        import db
        db.update_site_settings(tts_gpu_auth_token="gpu-secret-token")
        settings = db.get_site_settings()
        assert settings["tts_gpu_auth_token"] == "gpu-secret-token"

        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT tts_gpu_auth_token FROM site_settings WHERE id=1"
        ).fetchone()
        conn.close()
        raw = row["tts_gpu_auth_token"]
        assert raw.startswith("fernet:")
        assert raw != "gpu-secret-token"

    def test_a_gpu_token_stored_before_encryption_still_reads(self):
        """Existing installs hold this token in plaintext. It must keep
        working, and be encrypted the next time settings are saved."""
        import db
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.execute("UPDATE site_settings SET tts_gpu_auth_token=? WHERE id=1", ("plain-legacy-token",))
        conn.commit()
        conn.close()
        assert db.get_site_settings()["tts_gpu_auth_token"] == "plain-legacy-token"

        db.update_site_settings(tts_gpu_auth_token="plain-legacy-token")
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        raw = conn.execute("SELECT tts_gpu_auth_token FROM site_settings WHERE id=1").fetchone()[0]
        conn.close()
        assert raw.startswith("fernet:")
