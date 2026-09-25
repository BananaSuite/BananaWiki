import os
import sqlite3

import pytest


def test_backup_encryption_round_trip_and_tamper_detection(tmp_path, monkeypatch):
    from hosting import config
    from hosting.backup_crypto import decrypt_backup, encrypt_backup, is_encrypted_backup

    monkeypatch.setattr(config, "HOSTING_BACKUP_KEY_PATH", str(tmp_path / "key"))
    source = tmp_path / "platform.zip"
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    encrypted = encrypt_backup(str(source))
    assert is_encrypted_backup(encrypted)
    restored = tmp_path / "restored.zip"
    decrypt_backup(encrypted, str(restored))
    assert restored.read_bytes() == source.read_bytes()

    damaged = bytearray((tmp_path / "platform.zip.bwenc").read_bytes())
    damaged[len(damaged) // 2] ^= 1
    (tmp_path / "damaged.bwenc").write_bytes(damaged)
    with pytest.raises(Exception):
        decrypt_backup(str(tmp_path / "damaged.bwenc"), str(tmp_path / "bad.zip"))
    assert not (tmp_path / "bad.zip").exists()


def test_verification_and_password_reset_tokens_are_single_use(tmp_path, monkeypatch):
    from helpers._passwords import generate_password_hash
    from hosting import config
    from hosting.db import (
        consume_password_reset,
        create_account,
        get_account_by_id,
        init_hosting_db,
        issue_email_verification,
        issue_password_reset,
        update_hosting_account,
        verify_email_token,
    )

    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    init_hosting_db()
    account_id = create_account("verified-user", generate_password_hash("Password123"))
    update_hosting_account(account_id, email="person@example.com")
    future = "2999-01-01T00:00:00+00:00"
    issue_email_verification(account_id, "person@example.com", "verify-token", future)
    assert verify_email_token("verify-token")["email_verified_at"]
    assert verify_email_token("verify-token") is None

    issue_password_reset(account_id, "reset-token", future)
    assert consume_password_reset("reset-token", generate_password_hash("Changed123"))
    assert not consume_password_reset("reset-token", generate_password_hash("ChangedAgain123"))
    assert get_account_by_id(account_id)["session_version"] == 1


def test_schema_rebuild_preserves_newer_instance_metadata(tmp_path, monkeypatch):
    from hosting import config
    from hosting.db import get_hosting_db_context, init_hosting_db

    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT UNIQUE, "
        "password TEXT, is_admin INTEGER, created_at TEXT)"
    )
    raw.execute("INSERT INTO accounts VALUES ('a', 'owner', 'x', 1, 'now')")
    raw.execute(
        "CREATE TABLE instances (id TEXT PRIMARY KEY, account_id TEXT, "
        "subdomain TEXT NOT NULL UNIQUE COLLATE NOCASE, status TEXT, port INTEGER, "
        "created_at TEXT, expires_at TEXT, domain_mode TEXT DEFAULT 'hosting', "
        "declared_use_case TEXT DEFAULT '', tos_compliance_declared_at TEXT, "
        "oauth_client_id TEXT, oauth_client_secret_hash TEXT, easy_wiki INTEGER DEFAULT 0)"
    )
    raw.execute(
        "INSERT INTO instances VALUES "
        "('i','a','demo','running',6001,'now','2999','hosting',"
        "'Private engineering notes','now','client-id','secret-hash',1)"
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(db_path))
    init_hosting_db()
    with get_hosting_db_context() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id='i'").fetchone()
    assert row["declared_use_case"] == "Private engineering notes"
    assert row["oauth_client_id"] == "client-id"
    assert row["oauth_client_secret_hash"] == "secret-hash"
    assert row["easy_wiki"] == 1


def test_release_131_safe_defaults(tmp_path, monkeypatch):
    from hosting import config
    from hosting.db import get_hosting_settings, init_hosting_db

    monkeypatch.setattr(config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    init_hosting_db()
    settings = get_hosting_settings()
    assert settings["ask_email_new_signup"] == 0
    assert settings["ask_email_existing_users"] == 0
    assert settings["email_required"] == 0
    assert settings["email_verification_required"] == 0
    assert settings["global_limit_enabled"] == 1
    assert settings["forbid_non_admin_public_wikis"] == 1
