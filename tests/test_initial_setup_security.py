"""First-run ownership must be authorized and claimed atomically."""

from concurrent.futures import ThreadPoolExecutor


def test_initial_setup_requires_operator_token(client, monkeypatch):
    import config
    import db
    from app import app
    monkeypatch.setitem(app.config, "ENFORCE_SETUP_TOKEN", True)
    payload = {"username": "first-admin", "password": "Setup-password-123", "confirm_password": "Setup-password-123"}
    response = client.post("/setup", data=payload)
    assert response.status_code == 403
    assert db.get_user_by_username("first-admin") is None
    payload["setup_token"] = config.SETUP_TOKEN
    assert client.post("/setup", data=payload).status_code == 302
    assert db.get_user_by_username("first-admin") is not None


def test_setup_link_clears_token_from_browser_url(client, monkeypatch):
    import config
    from app import app
    monkeypatch.setitem(app.config, "ENFORCE_SETUP_TOKEN", True)
    response = client.get("/setup", query_string={"setup_token": config.SETUP_TOKEN})
    assert response.status_code == 302
    assert response.location.endswith("/setup")
    assert config.SETUP_TOKEN.encode() not in client.get("/setup").data


def test_initial_admin_creation_is_atomic(client):
    import db
    def create(index):
        try:
            return db.complete_initial_setup("admin-" + str(index), "test-hash")
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(value is not None for value in results) == 1
    with db.get_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT setup_done FROM site_settings WHERE id=1").fetchone()[0] == 1


def test_single_use_admin_invite_creates_only_one_account(client):
    import db
    from threading import Barrier
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    code = db.generate_invite_code(owner, max_uses=1, assigned_role="admin")
    ready = Barrier(2)

    def signup(index):
        ready.wait(timeout=10)
        try:
            return db.create_signup_user(f"invited-{index}", "test-hash", invite_code=code.lower())
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(signup, range(2)))
    assert sum(uid is not None for uid in results) == 1
    winner = next(uid for uid in results if uid)
    assert db.get_user_by_id(winner)["role"] == "admin"
    with db.get_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username LIKE 'invited-%'").fetchone()[0] == 1
        assert conn.execute("SELECT use_count FROM invite_codes WHERE code=?", (code,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM invite_code_usage WHERE user_id=?", (winner,)).fetchone()[0] == 1


def test_signup_rechecks_invite_after_password_hashing(client, monkeypatch):
    import db
    import routes.auth as auth
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    code = db.generate_invite_code(owner, max_uses=1, assigned_role="admin")

    def competing_signup(_password):
        winner = db.create_user("winning-signup", "test-hash", role="admin")
        assert db.use_invite_code(code, winner)
        return "test-hash"

    monkeypatch.setattr(auth, "generate_password_hash", competing_signup)
    response = client.post("/signup", data={
        "username": "losing-signup", "password": "Example-password-123",
        "confirm_password": "Example-password-123", "invite_code": code,
    })
    assert response.status_code in (200, 400)
    assert db.get_user_by_username("losing-signup") is None
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_signup_uses_current_invite_role(client, monkeypatch):
    import db
    import routes.auth as auth
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    code = db.generate_invite_code(owner, max_uses=1, assigned_role="admin")

    def change_invite_role(_password):
        with db.get_db_context() as conn:
            conn.execute("UPDATE invite_codes SET assigned_role='user' WHERE code=?", (code,))
            conn.commit()
        return "test-hash"

    monkeypatch.setattr(auth, "generate_password_hash", change_invite_role)
    response = client.post("/signup", data={
        "username": "ordinary-member", "password": "Example-password-123",
        "confirm_password": "Example-password-123", "invite_code": code,
    })
    assert response.status_code == 302
    assert db.get_user_by_username("ordinary-member")["role"] == "user"


def test_rejected_signup_does_not_consume_invite(client):
    import sqlite3
    import pytest
    import db
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    code = db.generate_invite_code(owner, max_uses=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_signup_user("invite-owner", "test-hash", invite_code=code)
    assert db.validate_invite_code(code)["use_count"] == 0
    assert db.create_signup_user("valid-new-member", "test-hash", invite_code=code)


def test_invite_redemption_rejects_invalid_expiry_and_suspended_creator(client):
    import pytest
    import db
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    for expiry in ("2000-01-01T00:00:00Z", "not-a-date"):
        code = db.generate_invite_code(owner, expires_at=expiry)
        assert db.validate_invite_code(code) is None
        with pytest.raises(ValueError):
            db.create_signup_user("rejected-member", "test-hash", invite_code=code)
        assert db.get_user_by_username("rejected-member") is None
    code = db.generate_invite_code(owner)
    with db.get_db_context() as conn:
        conn.execute("UPDATE users SET suspended=1 WHERE id=?", (owner,))
        conn.commit()
    with pytest.raises(ValueError):
        db.create_signup_user("rejected-member", "test-hash", invite_code=code)
    assert db.get_user_by_username("rejected-member") is None


def test_signup_requires_approval_before_account_becomes_visible(client):
    import db
    owner = db.complete_initial_setup("invite-owner", "test-hash")
    code = db.generate_invite_code(owner, assigned_role="admin")
    uid = db.create_signup_user("pending-member", "test-hash", invite_code=code, approval_required=True)
    assert db.get_user_by_id(uid)["approval_status"] == "pending"
    assert db.validate_invite_code(code) is None
