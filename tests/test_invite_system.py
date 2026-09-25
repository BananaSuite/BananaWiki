import pytest
import db
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash

def test_multi_use_invite_code(admin_user):
    """Test that a multi-use invite code works correctly."""
    # Create a code with 2 uses
    code = db.generate_invite_code(admin_user, max_uses=2)

    # Create users to use the code
    u1 = db.create_user("u1", generate_password_hash("pass"))
    u2 = db.create_user("u2", generate_password_hash("pass"))
    u3 = db.create_user("u3", generate_password_hash("pass"))

    # First use
    assert db.validate_invite_code(code) is not None
    assert db.use_invite_code(code, u1) is True

    # Second use
    assert db.validate_invite_code(code) is not None
    assert db.use_invite_code(code, u2) is True

    # Third use should fail
    assert db.validate_invite_code(code) is None
    assert db.use_invite_code(code, u3) is False

def test_unlimited_use_invite_code(admin_user):
    """Test that an unlimited invite code works multiple times."""
    # Create a code with 0 (unlimited) uses
    code = db.generate_invite_code(admin_user, max_uses=0)

    for i in range(10):
        uid = db.create_user(f"unlimited_{i}", generate_password_hash("pass"))
        assert db.validate_invite_code(code) is not None
        assert db.use_invite_code(code, uid) is True

def test_never_expires_invite_code(admin_user):
    """Test that a code with no expiration works."""
    code = db.generate_invite_code(admin_user, max_uses=1, expires_at=None)
    assert db.validate_invite_code(code) is not None

def test_expired_invite_code(admin_user):
    """Test that an expired code is invalid."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    code = db.generate_invite_code(admin_user, max_uses=1, expires_at=past)
    assert db.validate_invite_code(code) is None

def test_usage_tracking(admin_user):
    """Test that invite code usage is tracked correctly."""
    code_str = db.generate_invite_code(admin_user, max_uses=5)
    ua = db.create_user("user_a", generate_password_hash("pass"))
    ub = db.create_user("user_b", generate_password_hash("pass"))

    db.use_invite_code(code_str, ua)
    db.use_invite_code(code_str, ub)

    row = db.validate_invite_code(code_str)
    usage = db.get_invite_code_usage(row["id"])

    assert len(usage) == 2
    usernames = [u["username"] for u in usage]
    assert "user_a" in usernames
    assert "user_b" in usernames


def test_custom_invite_code_is_normalized_and_valid(admin_user):
    """Admins can create custom alphanumeric signup invite codes."""
    code = db.generate_invite_code(admin_user, max_uses=1, custom_code="welcome42")

    assert code == "WELCOME42"
    assert db.validate_invite_code("welcome42") is not None


def test_custom_invite_code_rejects_invalid_format(admin_user):
    """Custom signup invite codes must be alphanumeric and no longer than 32 chars."""
    with pytest.raises(ValueError):
        db.generate_invite_code(admin_user, custom_code="bad-code!")

    with pytest.raises(ValueError):
        db.generate_invite_code(admin_user, custom_code="A" * 33)


def test_custom_invite_code_rejects_case_insensitive_duplicate(admin_user):
    """Custom codes cannot collide with existing codes in any casing."""
    db.generate_invite_code(admin_user, custom_code="Launch2026")

    with pytest.raises(db.IntegrityError):
        db.generate_invite_code(admin_user, custom_code="launch2026")


def test_admin_can_generate_custom_invite_code(logged_in_admin):
    """POST /admin/codes/generate accepts a custom invite code."""
    resp = logged_in_admin.post(
        "/admin/codes/generate",
        data={"custom_code": "team2026", "max_uses": "2", "expiry_mode": "never"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"TEAM2026" in resp.data
    row = db.validate_invite_code("team2026")
    assert row is not None
    assert row["max_uses"] == 2


def test_admin_custom_invite_code_invalid_format_rejected(logged_in_admin):
    """Invalid custom invite codes are rejected before insert."""
    before = len(db.list_invite_codes(active_only=False))

    resp = logged_in_admin.post(
        "/admin/codes/generate",
        data={"custom_code": "bad-code!", "expiry_mode": "never"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert b"alphanumeric" in resp.data
    assert len(db.list_invite_codes(active_only=False)) == before


def test_signup_accepts_eight_character_custom_invite_code(client, admin_user):
    """Eight-character custom codes should not be hyphen-normalized on signup."""
    db.generate_invite_code(admin_user, custom_code="BANANA42")

    resp = client.post("/signup", data={
        "username": "custom8",
        "password": "password123",
        "confirm_password": "password123",
        "invite_code": "banana42",
    }, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Account created" in resp.data
