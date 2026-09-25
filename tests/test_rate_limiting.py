"""
Tests for BananaWiki rate limiting.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate limit store before and after each test."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def app_mod():
    """Return the app module (shared import for tests that need _RL_STORE)."""
    import app as _app_mod
    return _app_mod


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


# -----------------------------------------------------------------------
# _rl_check unit tests
# -----------------------------------------------------------------------
def test_rl_check_allows_within_limit():
    from app import _rl_check
    for _ in range(5):
        assert _rl_check("1.2.3.4", "bucket_a", 5, 60) is True


def test_rl_check_blocks_over_limit():
    from app import _rl_check
    for _ in range(5):
        _rl_check("1.2.3.4", "bucket_b", 5, 60)
    assert _rl_check("1.2.3.4", "bucket_b", 5, 60) is False


def test_rl_check_different_ips_are_independent():
    from app import _rl_check
    for _ in range(5):
        _rl_check("1.2.3.4", "bucket_c", 5, 60)
    # A different IP should have its own independent counter
    assert _rl_check("5.6.7.8", "bucket_c", 5, 60) is True


def test_rl_check_different_buckets_are_independent():
    from app import _rl_check
    for _ in range(5):
        _rl_check("1.2.3.4", "bucket_d1", 5, 60)
    # A different bucket for the same IP should be independent
    assert _rl_check("1.2.3.4", "bucket_d2", 5, 60) is True


# -----------------------------------------------------------------------
# In-memory rate limit store tests
# -----------------------------------------------------------------------
def test_rl_check_records_hit_in_memory():
    """_rl_check records a hit in the in-memory store on a permitted request."""
    from app import _rl_check, _RL_STORE
    result = _rl_check("9.9.9.9", "bucket_new", 5, 60)
    assert result is True
    assert len(_RL_STORE.get(("9.9.9.9", "bucket_new"), [])) == 1


def test_rl_store_plain_dict_not_defaultdict(app_mod):
    """_RL_STORE must be a plain dict subclass, not a defaultdict."""
    from collections import defaultdict
    assert not isinstance(app_mod._RL_STORE, defaultdict), (
        "_RL_STORE should not be a defaultdict"
    )


def test_rl_check_blocked_when_at_limit():
    """When the limit is reached, _rl_check returns False without adding another hit."""
    from app import _rl_check, _RL_STORE
    ip, bucket, max_req = "10.0.0.1", "bucket_retain", 3
    for _ in range(max_req):
        _rl_check(ip, bucket, max_req, 60)
    assert len(_RL_STORE.get((ip, bucket), [])) == max_req
    # This call should be blocked; hit count must not increase
    result = _rl_check(ip, bucket, max_req, 60)
    assert result is False
    assert len(_RL_STORE.get((ip, bucket), [])) == max_req


def test_rl_check_blocked_when_max_requests_zero():
    """When max_requests=0 every call is immediately blocked; no hit recorded."""
    from app import _rl_check, _RL_STORE
    ip, bucket = "10.0.0.2", "bucket_zero"
    result = _rl_check(ip, bucket, 0, 60)
    assert result is False
    assert len(_RL_STORE.get((ip, bucket), [])) == 0


def test_rl_check_expired_hits_not_counted():
    """Hits older than the window are ignored; a new request is permitted."""
    import time
    from collections import deque
    from app import _rl_check, _RL_STORE
    ip, bucket, max_req, window = "10.0.0.3", "bucket_stale", 3, 2
    # Pre-populate the in-memory store with old timestamps outside the window
    old_time = time.time() - window - 10
    _RL_STORE[(ip, bucket)] = deque([old_time] * max_req)
    # A new request should be allowed because old hits are outside the window
    result = _rl_check(ip, bucket, max_req, window)
    assert result is True
    # Old hits should be pruned, only the new one remains
    assert len(_RL_STORE[(ip, bucket)]) == 1


def test_rl_check_concurrent_requests_all_recorded():
    """Concurrent _rl_check calls are all handled correctly by the in-memory store."""
    import threading
    from app import _rl_check, _RL_STORE

    results = []
    ip, bucket, max_req, window = "10.0.0.4", "bucket_threads", 100, 60

    def make_request():
        results.append(_rl_check(ip, bucket, max_req, window))

    threads = [threading.Thread(target=make_request) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 50 requests should be allowed (well under max_req=100)
    assert all(results)
    # Exactly 50 hits should be recorded in memory
    assert len(_RL_STORE.get((ip, bucket), [])) == 50


def test_html_navigation_gets_do_not_consume_signup_route_limit(client, admin_user):
    """Browser page refreshes should not trip per-route buckets."""
    for _ in range(5):
        resp = client.get(
            "/signup",
            headers={"Accept": "text/html"},
        )
        assert resp.status_code != 429

    resp = client.post("/signup", data={
        "username": "user", "password": "pass",
        "confirm_password": "pass", "invite_code": "INVALID",
    })
    assert resp.status_code != 429


# -----------------------------------------------------------------------
# Signup rate limit
# -----------------------------------------------------------------------
def test_signup_rate_limited(client, admin_user):
    """Signup endpoint returns 429 after exceeding the per-IP limit."""
    for _ in range(10):
        client.post("/signup", data={
            "username": "user", "password": "pass",
            "confirm_password": "pass", "invite_code": "INVALID",
        })
    resp = client.post("/signup", data={
        "username": "user", "password": "pass",
        "confirm_password": "pass", "invite_code": "INVALID",
    })
    assert resp.status_code == 429
    assert b"Too Many Requests" in resp.data


# -----------------------------------------------------------------------
# API endpoint rate limits (JSON responses)
# -----------------------------------------------------------------------
def test_api_preview_rate_limited(logged_in_admin):
    """API preview endpoint returns JSON 429 after exceeding the limit."""
    for _ in range(30):
        logged_in_admin.post(
            "/api/preview",
            json={"content": "test"},
            content_type="application/json",
        )
    resp = logged_in_admin.post(
        "/api/preview",
        json={"content": "test"},
        content_type="application/json",
    )
    assert resp.status_code == 429
    data = resp.get_json()
    assert data is not None
    assert "error" in data


def test_upload_rate_limited(logged_in_admin):
    """Upload endpoint returns JSON 429 after exceeding the limit."""
    for _ in range(10):
        logged_in_admin.post(
            "/api/upload",
            data={},
            content_type="multipart/form-data",
        )
    resp = logged_in_admin.post(
        "/api/upload",
        data={},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 429
    data = resp.get_json()
    assert data is not None
    assert "error" in data


# -----------------------------------------------------------------------
# Global rate limit
# -----------------------------------------------------------------------
def test_global_rate_limit(client, admin_user, monkeypatch):
    """All non-static endpoints are blocked after hitting the global limit."""
    import app as app_mod
    monkeypatch.setattr(app_mod, "_RL_GLOBAL_MAX", 3)
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    for _ in range(3):
        client.get("/login")
    resp = client.get("/login")
    assert resp.status_code == 429
    assert b"Too Many Requests" in resp.data


def test_global_rate_limit_json_response(client, admin_user, monkeypatch):
    """Global rate limit on API paths returns JSON 429."""
    import app as app_mod
    monkeypatch.setattr(app_mod, "_RL_GLOBAL_MAX", 1)
    # Log in first (this consumes 1 request toward global)
    client.post("/login", data={"username": "admin", "password": "admin123"})
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    # Use one request
    client.get("/login")
    # Next request to an API path should be rate-limited with JSON
    resp = client.post(
        "/api/preview",
        json={"content": "test"},
        content_type="application/json",
    )
    assert resp.status_code == 429
    data = resp.get_json()
    assert data is not None
    assert "error" in data


def test_global_rate_limit_logged_in_user_renders_429(logged_in_admin, monkeypatch):
    """Global rate limit renders 429 page correctly for a logged-in user (sidebar uses categories)."""
    import app as app_mod
    monkeypatch.setattr(app_mod, "_RL_GLOBAL_MAX", 1)
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    logged_in_admin.get("/")
    resp = logged_in_admin.get("/")
    assert resp.status_code == 429
    assert b"Too Many Requests" in resp.data


# -----------------------------------------------------------------------
# 429 error handler
# -----------------------------------------------------------------------
def test_429_error_handler(client, admin_user, monkeypatch):
    """The 429 error handler renders the 429 template."""
    import app as app_mod
    monkeypatch.setattr(app_mod, "_RL_GLOBAL_MAX", 1)
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    client.get("/login")
    resp = client.get("/login")
    assert resp.status_code == 429
    assert b"429" in resp.data


# -----------------------------------------------------------------------
# Stale hit cleanup: expired hits are pruned from the in-memory store
# -----------------------------------------------------------------------
def test_rl_check_stale_hits_pruned_on_new_request():
    """After the window expires, a new request prunes old in-memory hits."""
    import time
    from collections import deque
    from app import _rl_check, _RL_STORE

    ip, bucket, max_req, window = "10.0.1.1", "bucket_cleanup", 3, 2

    # Pre-populate in-memory store with old timestamps outside the window.
    old_time = time.time() - window - 10
    _RL_STORE[(ip, bucket)] = deque([old_time] * max_req)

    # First new request: stale entries are pruned, fresh counter starts with 1.
    result = _rl_check(ip, bucket, max_req, window)
    assert result is True
    assert len(_RL_STORE[(ip, bucket)]) == 1


def test_rl_check_max_zero_records_no_hit():
    """When max_requests=0, _rl_check is blocked and records nothing."""
    from app import _rl_check, _RL_STORE
    ip, bucket = "10.0.1.2", "bucket_dead_code"
    result = _rl_check(ip, bucket, 0, 60)
    assert result is False
    assert len(_RL_STORE.get((ip, bucket), [])) == 0


# -----------------------------------------------------------------------
# prune_stale_rate_limit_hits: global cross-IP/bucket eviction
# -----------------------------------------------------------------------
def test_prune_stale_rate_limit_hits_removes_old_rows(isolated_db):
    """prune_stale_rate_limit_hits removes rows older than max_age_seconds
    across ALL ip+bucket combinations, not just the one being checked."""
    import sqlite3
    import db
    from datetime import datetime, timezone, timedelta

    max_age = 3600  # 1 hour

    # Insert stale rows for two different IPs and buckets
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=max_age + 10)).isoformat()
    conn = sqlite3.connect(config.DATABASE_PATH)
    for ip in ("1.1.1.1", "2.2.2.2"):
        conn.execute(
            "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (ip, "bucket_old", old_time),
        )
    conn.commit()
    conn.close()

    removed = db.prune_stale_rate_limit_hits(max_age_seconds=max_age)
    assert removed == 2

    # Verify no rows remain
    conn = sqlite3.connect(config.DATABASE_PATH)
    total = conn.execute("SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0]
    conn.close()
    assert total == 0


def test_prune_stale_rate_limit_hits_keeps_fresh_rows(isolated_db):
    """prune_stale_rate_limit_hits does NOT remove rows within max_age_seconds."""
    import sqlite3
    import db
    from datetime import datetime, timezone, timedelta

    max_age = 3600
    # Insert a recent row (well within the window)
    recent_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
        ("3.3.3.3", "bucket_fresh", recent_time),
    )
    conn.commit()
    conn.close()

    removed = db.prune_stale_rate_limit_hits(max_age_seconds=max_age)
    assert removed == 0

    conn = sqlite3.connect(config.DATABASE_PATH)
    total = conn.execute("SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0]
    conn.close()
    assert total == 1


def test_prune_stale_rate_limit_hits_mixed_rows(isolated_db):
    """prune_stale_rate_limit_hits removes only stale rows, leaving fresh ones."""
    import sqlite3
    import db
    from datetime import datetime, timezone, timedelta

    max_age = 3600
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=max_age + 100)).isoformat()
    recent_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    conn = sqlite3.connect(config.DATABASE_PATH)
    # 3 stale rows for unique IPs (simulating a past DDoS)
    for i in range(3):
        conn.execute(
            "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (f"10.0.0.{i}", "global", old_time),
        )
    # 2 fresh rows
    for i in range(2):
        conn.execute(
            "INSERT INTO rate_limit_hits (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (f"192.168.0.{i}", "global", recent_time),
        )
    conn.commit()
    conn.close()

    removed = db.prune_stale_rate_limit_hits(max_age_seconds=max_age)
    assert removed == 3

    conn = sqlite3.connect(config.DATABASE_PATH)
    remaining = conn.execute("SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0]
    conn.close()
    assert remaining == 2


def test_prune_stale_rate_limit_hits_returns_zero_on_empty_table(isolated_db):
    """prune_stale_rate_limit_hits returns 0 when the table is already empty."""
    import db
    removed = db.prune_stale_rate_limit_hits()
    assert removed == 0


def test_mini_cleanup_calls_prune_stale_rate_limit_hits(monkeypatch, client, admin_user):
    """The periodic mini-cleanup invokes prune_stale_rate_limit_hits."""
    import app as app_mod

    calls = []

    def fake_prune(max_age_seconds=3600):
        calls.append(max_age_seconds)
        return 0

    monkeypatch.setattr(app_mod.db, "prune_stale_rate_limit_hits", fake_prune)

    # The cleanup now runs in a background scheduler thread, but the
    # extracted helper is directly callable for verification.
    app_mod._run_periodic_cleanup_once()

    assert len(calls) >= 1, "prune_stale_rate_limit_hits was not called during mini-cleanup"


# -----------------------------------------------------------------------
# Chat polling endpoint rate limits
# -----------------------------------------------------------------------

@pytest.fixture
def _chat_poll_setup(client, admin_user):
    """Set up two users and a DM chat; return (client, chat_id)."""
    from werkzeug.security import generate_password_hash
    import db

    db.create_user("alice", generate_password_hash("alice123"), role="user")
    client.post("/login", data={"username": "admin", "password": "admin123"})
    # Create a DM chat between admin and alice
    resp = client.post("/chats/new", data={"username": "alice"}, follow_redirects=False)
    # Derive the chat_id from the redirect location (/chats/<id>)
    location = resp.headers.get("Location", "")
    chat_id = int(location.rstrip("/").split("/")[-1]) if "/chats/" in location else None
    if chat_id is None:
        admin = db.get_user_by_username("admin")
        chats = db.get_user_chats(admin["id"])
        chat_id = chats[0]["id"]
    return client, chat_id


def test_chat_messages_partial_rate_limited(_chat_poll_setup):
    """GET /chats/<id>/messages returns 429 after exceeding 60 req/60 s."""
    c, chat_id = _chat_poll_setup
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    for _ in range(60):
        c.get(f"/chats/{chat_id}/messages")
    resp = c.get(f"/chats/{chat_id}/messages")
    assert resp.status_code == 429


@pytest.fixture
def _group_poll_setup(client, admin_user):
    """Set up an admin user and a group chat; return (client, group_id)."""
    import db

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        "/groups/new",
        data={"name": "Test Group", "description": ""},
        follow_redirects=False,
    )
    location = resp.headers.get("Location", "")
    group_id = int(location.rstrip("/").split("/")[-1]) if "/groups/" in location else None
    if group_id is None:
        admin = db.get_user_by_username("admin")
        groups = db.get_user_groups(admin["id"])
        group_id = groups[0]["id"]
    return client, group_id


def test_group_messages_partial_rate_limited(_group_poll_setup):
    """GET /groups/<id>/messages returns 429 after exceeding 60 req/60 s."""
    c, group_id = _group_poll_setup
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    for _ in range(60):
        c.get(f"/groups/{group_id}/messages")
    resp = c.get(f"/groups/{group_id}/messages")
    assert resp.status_code == 429
