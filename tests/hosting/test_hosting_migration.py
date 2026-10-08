"""A 1.4 hosting.db (user_version 2) is taken over in place and keeps working."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from bananawiki.core.sqlite import DatabaseUnavailable
from bananawiki.hosting import migrations
from bananawiki.hosting.migrations import APPLICATION_ID, LATEST

from .hosting_support import build_portal, portal_environ

SECRET = "hosting-test-secret-" + "k" * 32
LEGACY_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def _legacy_fernet() -> Fernet:
    """1.4 derived the MFA key exactly like this (hosting/mfa.py)."""
    raw = hashlib.sha256((SECRET + "\0hosting-mfa-v1").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _legacy_database(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript((Path(migrations.__file__).with_name("baseline_v2.sql")).read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO hosting_settings (id, global_tts_gpu_auth_token, api_enabled) VALUES (1, 'gpu-secret', 1)")
    legacy_hash = generate_password_hash("legacy pass 1", method="pbkdf2:sha256:1000")
    conn.execute(
        "INSERT INTO accounts (id, username, password, is_admin, created_at, approved_by, totp_enabled, "
        "totp_secret_encrypted) VALUES ('a1', 'admin', ?, 1, '2025-01-02T03:04:05+00:00', '', 1, ?)",
        (legacy_hash, _legacy_fernet().encrypt(LEGACY_TOTP.encode()).decode()),
    )
    conn.execute(
        "INSERT INTO accounts (id, username, password, created_at, approved_by) "
        "VALUES ('a2', 'owner', ?, '2025-02-01T00:00:00', 'ghost')", (legacy_hash,),
    )
    conn.execute(
        "INSERT INTO instances (id, account_id, subdomain, status, port, admin_username, admin_password_plain, "
        "created_at, expires_at) VALUES ('i1', 'a2', 'alpha', 'running', 6001, 'admin_x', 'initial-secret', "
        "'2025-02-01T10:00:00+00:00', '2099-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO instance_suspension_audit (instance_id, action, performed_by, created_at) "
        "VALUES ('i1', 'suspend', 'system:storage-quota', '2025-03-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO instance_collaborators (instance_id, account_id, role, permissions, invited_by, created_at) "
        "VALUES ('i1', 'missing-account', 'full_access', '[]', 'a2', '2025-03-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO hosting_oauth_access_tokens (token_hash, client_id, account_id, expires_at) "
        "VALUES ('legacy-token', 'c', 'a2', '2099-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def legacy(tmp_path):
    environ = portal_environ(tmp_path)
    _legacy_database(Path(environ["HOSTING_DATABASE_PATH"]))
    return build_portal(tmp_path, environ=environ)


def _db(app):
    return sqlite3.connect(app.config["HOSTING"].database_path)


def test_upgrade_reaches_latest_version_and_keeps_identity(legacy):
    conn = _db(legacy)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    # The 1.4 updater reads exactly these columns.
    assert conn.execute("SELECT subdomain, domain_mode, status FROM instances").fetchall() == [("alpha", "hosting", "running")]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_upgrade_normalizes_timestamps_and_repairs_references(legacy):
    conn = _db(legacy)
    assert conn.execute("SELECT created_at FROM accounts WHERE id = 'a1'").fetchone()[0] == "2025-01-02 03:04:05"
    assert conn.execute("SELECT expires_at FROM instances").fetchone()[0] == "2099-01-01 00:00:00"
    assert conn.execute("SELECT approved_by FROM accounts WHERE id = 'a2'").fetchone()[0] is None
    assert conn.execute("SELECT performed_by FROM instance_suspension_audit").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM instance_collaborators").fetchone()[0] == 0


def test_upgrade_drops_legacy_oauth_tokens_and_adds_columns(legacy):
    conn = _db(legacy)
    assert conn.execute("SELECT COUNT(*) FROM hosting_oauth_access_tokens").fetchone()[0] == 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(instances)")}
    assert "oauth_client_secret_encrypted" in columns
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hosting_oauth_authorization_codes)")}
    assert {"code_challenge", "code_challenge_method"} <= columns


def test_plaintext_gpu_token_is_encrypted_at_rest(legacy):
    stored = _db(legacy).execute("SELECT global_tts_gpu_auth_token FROM hosting_settings").fetchone()[0]
    assert stored != "gpu-secret" and stored.startswith("fernet:")
    from bananawiki.hosting import settings
    from bananawiki.hosting.db import connection_scope

    with legacy.app_context(), connection_scope():
        assert settings.tts_gpu_token() == "gpu-secret"


def test_legacy_password_hash_and_totp_secret_still_sign_in(legacy):
    from bananawiki.hosting import mfa

    web = legacy.test_client()
    response = web.post("/login", data={"username": "admin", "password": "legacy pass 1"})
    assert response.headers["Location"].endswith("/login/mfa")
    with legacy.app_context():
        code = mfa.totp_code(LEGACY_TOTP)
    response = web.post("/login/mfa", data={"code": code})
    assert response.status_code == 302 and "/login" not in response.headers["Location"]
    assert web.get("/admin").status_code == 200


def test_legacy_initial_password_is_shown_once_then_erased(legacy):
    web = legacy.test_client()
    web.post("/login", data={"username": "owner", "password": "legacy pass 1"})
    first = web.get("/instances/i1").get_data(as_text=True)
    assert "initial-secret" in first
    assert _db(legacy).execute("SELECT admin_password_plain FROM instances").fetchone()[0] is None
    assert "initial-secret" not in web.get("/instances/i1").get_data(as_text=True)


def test_foreign_database_is_refused(tmp_path):
    environ = portal_environ(tmp_path)
    path = Path(environ["HOSTING_DATABASE_PATH"])
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA application_id = 1234")
    conn.execute("CREATE TABLE something (x)")
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseUnavailable):
        build_portal(tmp_path, environ=environ)


def test_fresh_database_starts_at_latest_with_settings_row(portal, query):
    assert query("SELECT signup_mode FROM hosting_settings WHERE id = 1", one=True)["signup_mode"] == "open"
    conn = sqlite3.connect(portal.config["HOSTING"].database_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST


def _schema(app) -> list[tuple]:
    conn = _db(app)
    try:
        return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                            "ORDER BY type, name").fetchall()
    finally:
        conn.close()


def test_fresh_and_upgraded_schemas_match(legacy, tmp_path):
    assert _schema(build_portal(tmp_path / "fresh")) == _schema(legacy)


def test_upgrade_keeps_invitations_and_merges_when_their_author_is_purged(tmp_path):
    environ = portal_environ(tmp_path)
    path = Path(environ["HOSTING_DATABASE_PATH"])
    _legacy_database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO accounts (id, username, password, created_at) VALUES ('a3', 'carol', 'x', "
                 "'2025-01-01 00:00:00')")
    conn.execute("INSERT INTO instance_collaborators (id, instance_id, account_id, role, permissions, invited_by, "
                 "created_at) VALUES (7, 'i1', 'a3', 'custom', '[\"view\"]', 'a1', '2025-03-01 00:00:00')")
    conn.execute("INSERT INTO hosting_account_merge_logs (id, target_account_id, source_account_id, merged_by, "
                 "instances_transferred, created_at) VALUES (4, 'a2', 'a3', 'a1', 2, '2025-04-01 00:00:00')")
    conn.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'instance_collaborators'")  # rows deleted since
    conn.commit()
    conn.close()
    conn = _db(build_portal(tmp_path, environ=environ))
    assert conn.execute("SELECT id, account_id, invited_by FROM instance_collaborators").fetchall() == [(7, "a3", "a1")]
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'instance_collaborators'").fetchone() == (40,)
    assert conn.execute("SELECT id, merged_by, instances_transferred FROM hosting_account_merge_logs").fetchall() == \
        [(4, "a1", 2)]
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_instance_collaborators_instance", "ix_instance_collaborators_account", "ix_merge_logs_target",
            "ix_merge_logs_source"} <= indexes
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM accounts WHERE id = 'a1'")
    assert conn.execute("SELECT account_id, invited_by FROM instance_collaborators").fetchall() == [("a3", None)]
    assert conn.execute("SELECT merged_by FROM hosting_account_merge_logs").fetchall() == [(None,)]
