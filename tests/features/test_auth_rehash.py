"""Re-hashing a password at sign-in keeps everything issued under it.

A password stored with another method or cost than the configured one is
re-hashed at the next sign-in. The password stays the same, so the account's
API tokens and the device cookies of its other browsers must keep working; a
real password change still retires both.
"""

from __future__ import annotations

import dataclasses

from conftest import PASSWORD

from bananawiki.core import passwords
from bananawiki.wiki.features.auth import signin

from .api_support import call, enable_api, issue

DEVICE_COOKIE = "bw_session_device"
NEW_METHOD = "pbkdf2:sha256:1001"


def _rehash_on_next_sign_in(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "BW", dataclasses.replace(app.config["BW"], password_hash_method=NEW_METHOD))


def _device_failures(db, user) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'login:device' AND ip LIKE ?",
                         (f"{user['id']}\n%",)))


def test_re_hashing_at_sign_in_keeps_the_accounts_api_tokens(app, client, db, make_user):
    enable_api(app, db)
    user = make_user("bob")
    legacy = passwords.hash_password(PASSWORD, "pbkdf2:sha256:600")
    db.execute("UPDATE users SET password = ?, api_access_enabled = 1 WHERE id = ?", (legacy, user["id"]))
    user = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    token = issue(app, user, ["categories"])
    api = app.test_client()
    assert call(api, "GET", "/categories", token).status_code == 200

    assert client.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)) != legacy, "re-hashed"
    assert call(api, "GET", "/categories", token).status_code == 200
    assert db.scalar("SELECT active FROM api_service__tokens WHERE user_id = ?", (user["id"],)) == 1


def test_a_real_password_change_still_retires_the_tokens(app, client, db, make_user):
    enable_api(app, db)
    user = make_user("bob")
    db.execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (user["id"],))
    token = issue(app, user, ["categories"])
    db.execute("UPDATE users SET password = ? WHERE id = ?",
               (passwords.hash_password("another password", "pbkdf2:sha256:1000"), user["id"]))
    assert call(app.test_client(), "GET", "/categories", token).status_code == 401


def test_re_hashing_at_sign_in_keeps_the_other_browsers_known_devices(app, db, make_user, monkeypatch):
    user = make_user("bob")
    laptop, phone = app.test_client(), app.test_client()
    assert laptop.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    laptop.post("/logout")
    before = db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],))

    _rehash_on_next_sign_in(app, monkeypatch)
    assert phone.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)) != before, "re-hashed"

    assert laptop.post("/login", data={"username": "bob", "password": "wrong password"}).status_code == 401
    assert _device_failures(db, user) == 1, "the laptop is still a known browser"


def test_a_real_password_change_still_retires_the_device_cookies(app, db, make_user):
    user = make_user("bob")
    laptop = app.test_client()
    assert laptop.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    laptop.post("/logout")
    db.execute("UPDATE users SET password = ? WHERE id = ?",
               (passwords.hash_password("another password", "pbkdf2:sha256:1000"), user["id"]))
    assert laptop.post("/login", data={"username": "bob", "password": "wrong password"}).status_code == 401
    assert _device_failures(db, user) == 0


def test_a_concurrent_re_hash_does_not_refuse_the_right_password(app, db, make_user, monkeypatch):
    """Two sign-ins verify the old hash; the one whose re-hash loses must still sign in."""
    make_user("bob")
    _rehash_on_next_sign_in(app, monkeypatch)
    original = signin._upgrade_hash
    raced = []

    def racing(account, password):
        if not raced:
            raced.append(True)
            # Another worker re-hashes the same password between the check and this re-hash.
            db.execute("UPDATE users SET password = ? WHERE id = ?",
                       (passwords.hash_password(PASSWORD, NEW_METHOD), account["id"]))
        return original(account, password)

    monkeypatch.setattr(signin, "_upgrade_hash", racing)
    browser = app.test_client()
    assert browser.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 302
    assert browser.get("/_probe/private").data == b"private"


def test_a_password_changed_during_the_check_is_not_signed_in(app, db, make_user, monkeypatch):
    make_user("bob")
    _rehash_on_next_sign_in(app, monkeypatch)
    original = signin._upgrade_hash

    def changed_meanwhile(account, password):
        db.execute("UPDATE users SET password = ? WHERE id = ?",
                   (passwords.hash_password("another password", NEW_METHOD), account["id"]))
        return original(account, password)

    monkeypatch.setattr(signin, "_upgrade_hash", changed_meanwhile)
    browser = app.test_client()
    assert browser.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401
    assert browser.get("/_probe/private").status_code == 302


class _ReHashBetweenReads:
    """The real ``db`` of the token module, except that another worker's sign-in re-hashes the
    owner's password between the token read and the owner read of ``tokens.authenticate``."""

    def __init__(self, real, re_hash):
        self._real = real
        self._re_hash = re_hash
        self.fired = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def one(self, sql, params=()):
        if not self.fired and sql.startswith("SELECT * FROM users WHERE id = ?"):
            self.fired = True
            self._re_hash()
        return self._real.one(sql, params)


def test_a_re_hash_between_the_token_and_owner_reads_keeps_the_token(app, db, make_user, monkeypatch):
    # The token's stamp read before the re-hash commits and the owner's new hash read after it
    # must not make the token look outdated and revoke it for good.
    from bananawiki.wiki.features.api_service import tokens

    enable_api(app, db)
    user = make_user("bob")
    legacy = passwords.hash_password(PASSWORD, "pbkdf2:sha256:600")
    db.execute("UPDATE users SET password = ?, api_access_enabled = 1 WHERE id = ?", (legacy, user["id"]))
    user = db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    token = issue(app, user, ["categories"])
    api = app.test_client()
    assert call(api, "GET", "/categories", token).status_code == 200

    skewed = _ReHashBetweenReads(tokens.db, lambda: signin._upgrade_hash(user, PASSWORD))
    monkeypatch.setattr(tokens, "db", skewed)
    response = call(api, "GET", "/categories", token)
    monkeypatch.undo()
    assert skewed.fired and db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)) != legacy
    assert response.status_code == 200
    assert db.scalar("SELECT active FROM api_service__tokens WHERE user_id = ?", (user["id"],)) == 1
    assert call(api, "GET", "/categories", token).status_code == 200
