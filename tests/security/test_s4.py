"""Regression tests for full re-audit pass 13, group s4 (SW3-2, SW3-3).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from bananawiki.core.timeutil import sql_in, utcnow
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.federation import protocol, sync
from bananawiki.wiki.features.pages import service as pages


def _enable(app, *features: str) -> None:
    with app.test_request_context(), connection_scope():
        for feature in features:
            registry.set_enabled(feature, True)


def _page(app, title: str, author_id=None):
    with app.test_request_context(), connection_scope():
        return pages.create(title, "confidential", author_id=author_id)


def _entries(db, start: int, prefix: str) -> list[dict]:
    rows = db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? "
                  "AND action LIKE ? ORDER BY id", (start, prefix + "%"))
    return [{**row, "details": json.loads(row["details"])} for row in rows]


def _mark(db) -> int:
    return db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")


# SW3-2


def test_sw3_2_schedules_and_their_removal_name_the_administrator(app_factory, make_user, login, db):
    app = app_factory()
    _enable(app, "temporary_accounts", "audit")
    admin = make_user("plainadmin", role="admin")
    make_user("owner1", role="owner")
    victim = make_user("victim", role="editor")
    make_user("promoted", role="editor")
    page = _page(app, "Secret plans", admin["id"])
    client = app.test_client()
    login(client, admin)
    start = _mark(db)
    when = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    assert client.post("/admin/temporary/user", data={"username": "victim", "expires_at": when}).status_code == 302
    assert client.post("/admin/temporary/role", data={"username": "promoted", "original_role": "user",
                                                      "expires_at": when}).status_code == 302
    assert client.post("/admin/temporary/visibility", data={"page_slug": page["slug"], "state": "hide",
                                                            "expires_at": when}).status_code == 302
    assert client.post("/admin/temporary/page", data={"page_slug": page["slug"],
                                                      "expires_at": when}).status_code == 302
    assert client.post(f"/admin/temporary/user/{victim['id']}/remove").status_code == 302
    rows = _entries(db, start, "temporary.")
    assert [(row["action"], row["details"]["kind"]) for row in rows] == [
        ("temporary.scheduled", "user"), ("temporary.scheduled", "role"), ("temporary.scheduled", "visibility"),
        ("temporary.scheduled", "page"), ("temporary.removed", "user")]
    assert {row["actor_id"] for row in rows} == {admin["id"]}
    assert rows[0]["details"]["username"] == "victim" and rows[0]["details"]["expires_at"]
    assert rows[1]["details"]["new_role"] == "user"
    assert rows[2]["details"]["hidden"] is True and rows[2]["target_id"] == str(page["id"])


def test_sw3_2_the_job_names_whoever_set_the_schedule(app_factory, make_user, login, db):
    app = app_factory()
    _enable(app, "temporary_accounts", "audit")
    admin = make_user("plainadmin", role="admin")
    make_user("owner1", role="owner")
    victim = make_user("victim", role="editor")
    promoted = make_user("promoted", role="editor")
    hidden = _page(app, "Hidden for a while", admin["id"])
    doomed = _page(app, "Doomed", admin["id"])
    client = app.test_client()
    login(client, admin)
    when = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    for url, data in (("/admin/temporary/user", {"username": "victim"}),
                      ("/admin/temporary/role", {"username": "promoted", "original_role": "user"}),
                      ("/admin/temporary/visibility", {"page_slug": hidden["slug"], "state": "hide"}),
                      ("/admin/temporary/page", {"page_slug": doomed["slug"]})):
        assert client.post(url, data={**data, "expires_at": when}).status_code == 302
    for table in ("temp_users", "temp_roles", "temp_page_index_state", "temp_pages"):
        db.execute(f"UPDATE {table} SET expires_at = ?", (sql_in(minutes=-1),))
    start = _mark(db)
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (victim["id"],)) == 0
    assert db.scalar("SELECT role FROM users WHERE id = ?", (promoted["id"],)) == "user"
    rows = _entries(db, start, "temporary.carried_out")
    assert sorted(row["details"]["kind"] for row in rows) == ["page", "role", "user", "visibility"]
    assert {row["actor_id"] for row in rows} == {admin["id"]}
    assert {row["details"]["via"] for row in rows} == {"temporary_accounts.expire"}
    # The job's own role history entry still names nobody, so it is not taken for a manual change.
    assert db.scalar("SELECT changed_by FROM role_history WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                     (promoted["id"],)) is None


# SW3-3


def test_sw3_3_federation_actions_are_audited(app_factory, make_user, login, db, monkeypatch):
    app = app_factory(environ={"BW_FEDERATION_ENABLED": "1"})
    _enable(app, "audit")
    page = _page(app, "Secret page")
    admin = make_user("fedadmin", role="admin")
    client = app.test_client()
    login(client, admin)

    def refuse(*_args, **_kwargs):
        raise protocol.ProtocolError("federation.error.remote_status")

    monkeypatch.setattr(sync, "fetch", refuse)  # never leave localhost
    start = _mark(db)
    peer = str(uuid.uuid4())
    assert client.post("/admin/federation", data={
        "wiki_id": peer, "name": "Partner", "base_url": "https://partner.example:8443",
        "secret": "cd" * 32, "category": "", "allow_private_network": "1"}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM federation_peers") == 1
    assert client.post("/federation/sharing", data={"peer_id": peer, "slug": page["slug"]}).status_code == 302
    assert client.post("/federation/sharing", data={"peer_id": peer, "action": "unshare",
                                                    "page_id": str(page["id"])}).status_code == 302
    assert client.post("/federation/sharing", data={"peer_id": peer, "slug": page["slug"]}).status_code == 302
    assert client.post(f"/admin/federation/{peer}/sync").status_code == 302
    assert client.post(f"/admin/federation/{peer}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM federation_peers") == 0
    rows = _entries(db, start, "federation.")
    assert [row["action"] for row in rows] == [
        "federation.paired", "federation.page_shared", "federation.page_unshared", "federation.page_shared",
        "federation.synced", "federation.disconnected"]
    assert {row["actor_id"] for row in rows} == {admin["id"]}
    paired = rows[0]["details"]
    assert paired["host"] == "partner.example" and paired["private_network"] is True
    assert "cd" * 32 not in json.dumps(rows)
    assert rows[1]["target_id"] == str(page["id"]) and rows[1]["details"]["wiki_id"] == peer
    assert rows[4]["details"]["stored"] is False
    assert rows[5]["details"]["shares_removed"] == 1
