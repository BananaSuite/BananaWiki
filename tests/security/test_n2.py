"""Regression tests for the NW1 findings of the full re-audit pass 8 handled by n2 (REST API)."""

from __future__ import annotations

import json
import logging

import pytest

from tests.features.api_support import call, enable_api, issue

# NW1-2: refused calls count against the per-account API limit, and 429s are logged once per window


def _calls(db) -> list[int]:
    return [row["status_code"] for row in db.all("SELECT status_code FROM api_service__audit_log ORDER BY id")]


def test_nw1_2_calls_refused_for_scope_count_against_the_limit(app, make_user, db):
    enable_api(app, db)
    db.execute("UPDATE site_settings SET api_service_rate_limit = 3 WHERE id = 1")
    user = make_user("scripted", role="user", api_access_enabled=1)
    token = issue(app, user, scopes=("pages",), write=False)
    client = app.test_client()

    writes = [call(client, "POST", "/pages", token, json={"title": "t"}).status_code for _ in range(5)]
    admin_only = [call(client, "GET", "/users/" + "y" * 600, token).status_code for _ in range(5)]
    assert writes == [403, 403, 403, 429, 429], "a refusal for scope uses up the per-account budget"
    assert admin_only == [429] * 5
    reads = call(client, "GET", "/pages", token)
    assert reads.status_code == 429 and reads.headers["X-RateLimit-Remaining"] == "0"
    assert _calls(db) == [403, 403, 403, 429], "past the limit, one refusal per token and window is kept"


def test_nw1_2_a_429_is_logged_again_in_the_next_window(app, make_user, db):
    enable_api(app, db)
    db.execute("UPDATE site_settings SET api_service_rate_limit = 1 WHERE id = 1")
    user = make_user("patient", role="user", api_access_enabled=1)
    token = issue(app, user, scopes=("pages",))
    client = app.test_client()
    assert [call(client, "GET", "/pages", token).status_code for _ in range(4)] == [200, 429, 429, 429]
    # Let the window pass for both the budget and the record of refusals.
    db.execute("UPDATE rate_limit_hits SET hit_at = '2000-01-01 00:00:00'")
    assert [call(client, "GET", "/pages", token).status_code for _ in range(3)] == [200, 429, 429]
    assert _calls(db) == [200, 429, 200, 429]


# NW1-4: a deeply nested body sent with an Idempotency-Key to the account creation calls answers 400, not 500

DEEP = b"[" * 200_000 + b"]" * 200_000


@pytest.mark.parametrize("path", ["/users", "/users/bulk"])
def test_nw1_4_deep_json_with_idempotency_key_is_refused_not_a_500(app, make_user, db, caplog, path):
    enable_api(app, db)
    admin = make_user("boss", role="admin")
    token = issue(app, admin, scopes=("users",))
    client = app.test_client()
    with caplog.at_level(logging.ERROR):
        answers = [call(client, "POST", path, token, data=DEEP, content_type="application/json",
                        headers={"Idempotency-Key": key}) for key in ("deep-1", "deep-2")]
    assert [answer.status_code for answer in answers] == [400, 400]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_nw1_4_idempotent_account_creation_still_uses_the_early_hash(app, make_user, db):
    enable_api(app, db)
    admin = make_user("boss", role="admin")
    token = issue(app, admin, scopes=("users",))
    answer = call(app.test_client(), "POST", "/users", token, json={"username": "keyed", "password": "a-long-pass-1234"},
                  headers={"Idempotency-Key": "create-keyed"})
    assert answer.status_code == 201
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'keyed'") == 1


# NW1-5: site settings changed through PUT /api/v1/settings are recorded in the site audit log


def _settings_entries(db) -> list[dict]:
    return [json.loads(row["details"]) for row in
            db.all("SELECT details FROM audit_log WHERE action = 'settings.updated' ORDER BY id")]


def test_nw1_5_settings_changed_through_the_api_are_audited(app, make_user, db):
    enable_api(app, db)
    admin = make_user("plain_admin", role="admin")
    token = issue(app, admin, scopes=("settings",))
    client = app.test_client()
    answer = call(client, "PUT", "/settings", token, json={
        "public_mode": 1, "open_signup": 1, "approval_required": 0, "bot_protection_enabled": 0,
        "page_builder_enabled": 1, "session_limit_enabled": 1})
    assert answer.status_code == 200
    changed = answer.get_json()["updated"]
    assert "page_builder_enabled" in changed and "public_mode" in changed
    entries = _settings_entries(db)
    assert entries == [{"changed": ", ".join(changed), "via": "api"}]
    row = db.one("SELECT actor_id FROM audit_log WHERE action = 'settings.updated'")
    assert row["actor_id"] == str(admin["id"])


def test_nw1_5_unchanged_or_refused_settings_are_not_audited(app, make_user, db):
    enable_api(app, db)
    admin = make_user("plain_admin", role="admin")
    token = issue(app, admin, scopes=("settings",))
    client = app.test_client()
    current = call(client, "GET", "/settings", token).get_json()["settings"]
    assert call(client, "PUT", "/settings", token, json={"public_mode": current["public_mode"]}).status_code == 200
    assert call(client, "PUT", "/settings", token, json={"public_mode": 1, "no_such_key": 1}).status_code == 400
    assert _settings_entries(db) == []
