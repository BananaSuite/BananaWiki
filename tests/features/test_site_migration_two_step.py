"""Site import and two-step verification; importing the same wiki twice.

Authenticator secrets and recovery codes are bound to the secret key of the
wiki that stored them, and an archive never holds that key. An import keeps
the two-step set-up of the accounts whose secrets this wiki can read and turns
it off, with a notice, for the others, who would otherwise be locked out.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

import pytest

from bananawiki.core import totp
from bananawiki.wiki.features.auth import two_step

from .test_auth_two_step import password_step, signed_in
from .test_site_admin_migration import admin_client_for, do_import, export, query, raw_db, zip_bytes

FOREIGN_KEY = "the secret key of another wiki " + "y" * 32
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def owner_client(app, login):
    return admin_client_for(app, login, "site_owner")


@pytest.fixture
def source(admin, owner_client):
    """An export of a wiki with an owner (``site_owner``) and an administrator (``admin_user``)."""
    response = export(owner_client)
    assert response.status_code == 200, response.data[:300]
    data = response.get_data()
    response.close()
    return data


@pytest.fixture
def target(app_factory, tmp_path, login):
    other = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / "target")})
    return other, admin_client_for(other, login, "target_admin")


def _with_two_step(archive: bytes, tmp_path, rows: dict[str, str]) -> bytes:
    """*archive* with two-step verification on for each username in *rows*, encrypted with that secret key."""
    path = tmp_path / "two-step.db"
    path.write_bytes(raw_db(archive))
    conn = sqlite3.connect(path, isolation_level=None)
    for username, key in rows.items():
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        encrypted = totp.encrypt_secret(totp.cipher(key, two_step._CIPHER_PURPOSE), SECRET)
        conn.execute("INSERT INTO auth__two_step (user_id, secret_encrypted, recovery_hashes, last_counter, "
                     "enabled_at) VALUES (?, ?, '[]', -1, datetime('now'))", (user_id, encrypted))
    conn.close()
    return zip_bytes({"manifest.json": b'{"format_version": 1}', "bananawiki.db": path.read_bytes()})


def test_an_import_turns_off_two_step_this_wiki_cannot_read_and_says_so(source, target, tmp_path):
    other, client = target
    own_key = other.config["BW"].secret_key
    archive = _with_two_step(source, tmp_path, {"site_owner": FOREIGN_KEY, "admin_user": own_key})
    response = do_import(client, archive)
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    on = {row["username"] for row in query(other, "SELECT u.username FROM auth__two_step t "
                                                  "JOIN users u ON u.id = t.user_id")}
    assert on == {"admin_user"}, "only the set-up this wiki can read is kept"
    entry = query(other, "SELECT details FROM audit_log WHERE action = 'site.import_two_step_off'")
    assert len(entry) == 1 and "site_owner" in entry[0]["details"] and "admin_user" not in entry[0]["details"]
    page = client.get("/login").get_data(as_text=True)
    assert "Two-step verification was turned off" in page and "site_owner" in page

    owner = other.test_client()
    response = password_step(owner, {"username": "site_owner"})
    assert response.status_code == 302 and not response.headers["Location"].endswith("/login/two-step")
    assert signed_in(owner), "the importing owner is not locked out"


def test_the_notice_is_translated(source, target, tmp_path):
    other, client = target
    archive = _with_two_step(source, tmp_path, {"site_owner": FOREIGN_KEY})
    client.get("/admin/migration?lang=it")  # the notice is written in the language of the import
    do_import(client, archive)
    page = client.get("/login").get_data(as_text=True)
    assert "La verifica in due passaggi è stata disattivata" in page


def test_importing_an_export_of_the_same_wiki_keeps_each_audit_entry_once(app, source, owner_client):
    columns = "created_at, actor_id, action, target_type, target_id, ip, details"
    before = Counter(tuple(row.values()) for row in query(app, f"SELECT {columns} FROM audit_log"))
    assert do_import(owner_client, source).status_code == 302
    after = Counter(tuple(row.values()) for row in query(app, f"SELECT {columns} FROM audit_log"))
    assert {entry: after[entry] for entry in before} == dict(before), "nothing doubled"
    assert [entry for entry in after if entry[2] == "site.exported"], "what happened after the export is kept"
