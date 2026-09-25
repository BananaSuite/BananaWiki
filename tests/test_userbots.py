"""Tests for Userbot automation mode and API."""

import re

import pytest

import db


@pytest.fixture(autouse=True)
def enable_api_service_settings():
    """Userbot automation is owned by the API Service plugin."""
    db.update_site_settings(api_service_enabled=1)


def _extract_userbot_token(resp_data):
    html = resp_data.decode("utf-8", errors="ignore")
    m = re.search(r'id="userbot-api-token">([^<]+)<', html)
    return m.group(1).strip() if m else None


def _make_userbot_token(user_id):
    """Create an API service token with userbot scope for testing."""
    return db.create_api_token(
        user_id, name="userbot",
        permissions={"read": True, "write": True, "scopes": ["userbot"]}
    )


def test_enable_userbot_mode_generates_token(client, regular_user, admin_user):
    db.update_user(regular_user, api_access_enabled=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    resp = client.post(
        "/settings/api-tokens/userbot",
        data={"enable": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Userbot automation mode has been successfully enabled" in resp.data
    assert b"Copy your Userbot API key now" in resp.data
    user = db.get_user_by_id(regular_user)
    assert user["userbot_enabled"] == 1
    assert db.get_userbot_token_info(regular_user) is not None

    followup = client.get("/settings/api-tokens")
    assert b'id="userbot-api-token"' not in followup.data


def test_disable_userbot_mode_revokes_token(client, regular_user, admin_user):
    db.update_user(regular_user, api_access_enabled=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    resp = client.post(
        "/settings/api-tokens/userbot",
        data={"enable": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Userbot automation mode has been successfully disabled" in resp.data
    user = db.get_user_by_id(regular_user)
    assert user["userbot_enabled"] == 0
    assert db.get_userbot_token_info(regular_user) is None


def test_profile_shows_automated_badge_when_enabled(client, admin_user, regular_user):
    db.update_user(regular_user, api_access_enabled=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    resp = client.get("/users/user")
    assert resp.status_code == 200
    assert b"Automated Account" in resp.data


def test_userbot_api_me_and_profile_update(client, regular_user, admin_user):
    db.update_user(regular_user, api_access_enabled=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    enable = client.post(
        "/settings/api-tokens/userbot",
        data={"enable": "1"},
        follow_redirects=True,
    )
    token = _extract_userbot_token(enable.data)
    assert token

    me = client.get(
        "/api/v1/userbot/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200
    payload = me.get_json()
    assert payload["ok"] is True
    assert payload["user"]["id"] == regular_user

    update = client.post(
        "/api/v1/userbot/profile",
        json={"real_name": "Bot User", "bio": "updated by bot", "page_published": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert update.status_code == 200
    profile = db.get_user_profile(regular_user)
    assert profile["real_name"] == "Bot User"
    assert profile["bio"] == "updated by bot"
    assert profile["page_published"] == 1


def test_userbot_api_rejects_invalid_token(client, admin_user):
    resp = client.get("/api/v1/userbot/me", headers={"Authorization": "Bearer invalid"})
    assert resp.status_code == 401


def test_admin_can_force_disable_userbot_and_lock(client, admin_user, regular_user):
    db.update_user(regular_user, api_access_enabled=1)
    client.post("/login", data={"username": "user", "password": "user123"})
    client.post("/settings/api-tokens/userbot", data={"enable": "1"})
    client.get("/logout")

    client.post("/login", data={"username": "admin", "password": "admin123"})
    resp = client.post(
        f"/admin/users/{regular_user}/edit",
        data={"action": "userbot_lock", "lock_mode": "force_disabled"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    target = db.get_user_by_id(regular_user)
    assert target["userbot_enabled"] == 0
    assert target["userbot_mode_lock"] == "force_disabled"
    assert db.get_userbot_token_info(regular_user) is None

    client.get("/logout")
    client.post("/login", data={"username": "user", "password": "user123"})
    denied = client.post(
        "/settings/api-tokens/userbot",
        data={"enable": "1"},
        follow_redirects=True,
    )
    assert b"locked by an admin" in denied.data
    target = db.get_user_by_id(regular_user)
    assert target["userbot_enabled"] == 0
