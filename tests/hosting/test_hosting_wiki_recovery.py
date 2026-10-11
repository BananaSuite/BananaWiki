"""The portal's "reset admin password" is the way back into a hosted wiki, two-step verification included.

A hosted wiki's owner who lost both the authenticator app and the recovery
codes has no shell in the tenant to run ``bananawiki reset-two-step``: the
portal's reset turns two-step verification off for the account it resets,
ends its sessions, and records it in the wiki's audit log and the portal's.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .agent_fakes import make_runtime, make_spec, provision


def _db(runtime, name: str = "acme") -> sqlite3.Connection:
    conn = sqlite3.connect(Path(runtime._cfg().instances_dir) / name / "bananawiki.db")
    conn.row_factory = sqlite3.Row
    return conn


def _turn_on_two_step(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute("INSERT INTO auth__two_step (user_id, secret_encrypted, recovery_hashes, last_counter, enabled_at) "
                 "VALUES (?, 'sealed', '[]', 0, '2026-01-01 00:00:00')", (user_id,))


def test_resetting_the_admin_password_turns_off_that_accounts_two_step_verification(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    runtime.set_user_password(spec, "member", "member-password", "user")
    with _db(runtime) as conn:
        ids = {row["username"]: row["id"] for row in conn.execute("SELECT username, id FROM users")}
        _turn_on_two_step(conn, ids["owner1"])
        _turn_on_two_step(conn, ids["member"])
    assert runtime.reset_admin_password(spec, "owner1", "fresh-password-9") == "owner1"
    with _db(runtime) as conn:
        assert [row["user_id"] for row in conn.execute("SELECT user_id FROM auth__two_step")] == [ids["member"]]
        audit = conn.execute("SELECT actor_id, target_type, target_id, details FROM audit_log "
                             "WHERE action = 'user.two_step_reset'").fetchall()
    assert [(row["actor_id"], row["target_type"], row["target_id"]) for row in audit] == [(None, "user", ids["owner1"])]
    assert json.loads(audit[0]["details"]) == {"username": "owner1", "via": "hosting portal"}


def test_a_reset_of_every_administrator_turns_off_theirs_too(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    runtime.set_user_password(spec, "admin2", "admin-password", "admin")
    with _db(runtime) as conn:
        ids = {row["username"]: row["id"] for row in conn.execute("SELECT username, id FROM users")}
        _turn_on_two_step(conn, ids["admin2"])
    assert runtime.reset_admin_password(spec, "renamed-away", "fresh-password-8") == "owner1"
    with _db(runtime) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auth__two_step").fetchone()[0] == 0
        targets = [row[0] for row in conn.execute("SELECT target_id FROM audit_log "
                                                  "WHERE action = 'user.two_step_reset'")]
    assert targets == [ids["admin2"]], "only an account that had it on gets an entry"


def test_no_audit_entry_while_the_wikis_audit_log_is_switched_off(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    spec = make_spec()
    provision(runtime, spec)
    with _db(runtime) as conn:
        owner = conn.execute("SELECT id FROM users WHERE username = 'owner1'").fetchone()[0]
        _turn_on_two_step(conn, owner)
        conn.execute("INSERT INTO plugins (id, name, version, enabled) VALUES ('audit', 'Audit', '1', 0) "
                     "ON CONFLICT(id) DO UPDATE SET enabled = 0")
    runtime.reset_admin_password(spec, "owner1", "fresh-password-9")
    with _db(runtime) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auth__two_step").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.two_step_reset'").fetchone()[0] == 0


def test_the_portal_says_the_reset_turns_off_two_step_verification(portal, web, make_account, login, make_wiki, query):
    owner = make_account()
    wiki = make_wiki(owner)
    login(web, owner)
    page = web.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "two-step verification" in page
    response = web.post(f"/instances/{wiki['id']}/reset-password")
    assert response.status_code == 200
    assert "Two-step verification" in response.get_data(as_text=True)
    event = query("SELECT actor_id, reason FROM hosting_events WHERE action = 'instance.admin_password_reset'",
                  one=True)
    assert event["actor_id"] == owner["id"] and "two-step" in event["reason"]
    web.post("/language", data={"language": "it"})
    italian = web.post(f"/instances/{wiki['id']}/reset-password").get_data(as_text=True)
    assert "verifica in due passaggi" in italian
