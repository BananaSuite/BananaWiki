"""Regression tests for the security and stability review (password limits, impersonation, imports, jobs)."""

from __future__ import annotations

import io
import json
import os
import time
import zipfile

import pytest
from conftest import PASSWORD

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.db import connection_scope


def _run(app, check):
    with app.test_request_context(), connection_scope():
        return check()


# Password guesses are counted everywhere a signed-in account re-enters its password


def test_wrong_passwords_when_confirming_a_merge_are_counted(app, make_user, login, db):
    from bananawiki.wiki.features.users.service import PASSWORD_CHECKS

    requester, victim = make_user("requester"), make_user("victim")
    first = app.test_client()
    login(first, requester)
    first.post("/settings/merge-request", data={"other_username": "victim", "direction": "into_mine",
                                                "reason": "", "password": PASSWORD})
    merge_id = db.scalar("SELECT id FROM account_merge_requests")
    holder = app.test_client()
    login(holder, victim)
    for _ in range(PASSWORD_CHECKS):
        holder.post(f"/settings/merge/approve/{merge_id}", data={"password": "a wrong guess"})
    assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE bucket = 'account:password'") >= PASSWORD_CHECKS
    holder.post(f"/settings/merge/approve/{merge_id}", data={"password": PASSWORD})
    assert db.scalar("SELECT source_approved FROM account_merge_requests WHERE id = ?", (merge_id,)) == 0, \
        "the right password after the limit is refused too"


def test_a_suspended_account_cannot_guess_its_password_without_limit(app, make_user, login, db):
    from bananawiki.wiki.features.users.service import PASSWORD_CHECKS

    user = make_user("suspended_one")
    client = app.test_client()
    login(client, user)
    db.execute("UPDATE site_settings SET suspended_account_deletion_enabled = 1")
    db.execute("UPDATE users SET suspended = 1 WHERE id = ?", (user["id"],))
    for _ in range(PASSWORD_CHECKS):
        client.post("/account-suspended/delete", data={"password": "a wrong guess"})
    client.post("/account-suspended/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1


# Impersonation never acts on the impersonated account itself


def test_an_impersonated_owner_cannot_be_made_to_step_down(app, make_user, login, db):
    superuser = make_user("super", role="admin", is_superuser=1)
    owner, _other_owner = make_user("owner_one", role="owner"), make_user("owner_two", role="owner")
    client = app.test_client()
    login(client, superuser)
    assert client.post(f"/admin/users/{owner['id']}/impersonate").status_code == 302
    client.post(f"/admin/users/{owner['id']}/assign-role", data={"identity": "std:admin"})
    client.post(f"/admin/users/{owner['id']}/rename", data={"username": "renamed"})
    row = db.one("SELECT role, username FROM users WHERE id = ?", (owner["id"],))
    assert row == {"role": "owner", "username": "owner_one"}


def test_actions_while_impersonating_are_recorded_as_the_real_administrator(app, make_user, login, db):
    superuser = make_user("super", role="admin", is_superuser=1)
    owner, member = make_user("owner_one", role="owner"), make_user("member")
    client = app.test_client()
    login(client, superuser)
    client.post(f"/admin/users/{owner['id']}/impersonate")
    client.post(f"/admin/users/{member['id']}/assign-role", data={"identity": "std:editor"})
    assert db.scalar("SELECT role FROM users WHERE id = ?", (member["id"],)) == "editor"
    assert db.scalar("SELECT changed_by FROM role_history WHERE user_id = ?", (member["id"],)) == superuser["id"]


# Imports


def _canvas_editor(app, make_user, login, db):
    from bananawiki.wiki import registry

    _run(app, lambda: registry.set_enabled("canvas", True))
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'all' WHERE id = 1")
    client = app.test_client()
    login(client, make_user("drawer", role="editor"))
    return client


def test_a_canvas_bundle_with_bzip2_members_is_refused(app, make_user, login, db):
    client = _canvas_editor(app, make_user, login, db)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_BZIP2) as archive:
        archive.writestr("canvas.json", json.dumps({"title": "Bomb", "data": {"nodes": [], "edges": []}}))
    client.post("/canvas/import", data={"import_file": (io.BytesIO(bundle.getvalue()), "b.canvas.zip")},
                content_type="multipart/form-data")
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts") == 0


def test_an_oversized_page_id_in_a_canvas_is_dropped_and_old_ones_do_not_break_it(app, make_user, login, db):
    from bananawiki.wiki.features.canvas import model, service

    huge = "9" * 30
    node = model.clean_node({"id": "n1", "type": "wiki_page", "x": 0, "y": 0, "page_id": huge})
    assert "page_id" not in node
    client = _canvas_editor(app, make_user, login, db)
    owner = db.one("SELECT * FROM users WHERE username = 'drawer'")
    layout = _run(app, lambda: service.create("Board", "", owner["id"]))
    stored = {"nodes": [{"id": "n1", "type": "wiki_page", "x": 0, "y": 0, "w": 200, "h": 100, "page_id": int(huge)}],
              "edges": []}
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (json.dumps(stored), layout["id"]))
    assert client.get(f"/canvas/{layout['slug']}/data").status_code == 200


def test_a_page_builder_block_cannot_name_an_oversized_category():
    from bananawiki.wiki.features.page_builder import document

    with pytest.raises(document.DocumentError):
        document._pages({"source": "category", "category_id": 10 ** 30}, True, 1)


@pytest.mark.parametrize("columns", [5, {"a": 1}, [{"title": "c", "tickets": 5}],
                                     [{"title": "c", "tickets": [{"title": "t", "attachments": 3}]}],
                                     [{"title": "c", "tickets": [{"title": "t", "assignee_usernames": 7}]}]])
def test_a_kanban_import_with_odd_fields_is_refused_or_imported_never_a_500(admin_client, columns):
    document = {"title": "Odd", "columns": columns}
    response = admin_client.post("/kanban/import", data={
        "import_file": (io.BytesIO(json.dumps(document).encode()), "b.kanban.json")},
        content_type="multipart/form-data")
    assert response.status_code < 500


def test_reading_the_first_heading_takes_linear_time():
    from bananawiki.wiki.features.page_export import markdown_files

    started = time.perf_counter()
    assert markdown_files.first_heading("# a" + " " * 400_000 + "b") == "a" + " " * 400_000 + "b"
    assert time.perf_counter() - started < 1.0
    assert markdown_files.first_heading("# C#\n") == "C#"
    assert markdown_files.first_heading("\n# Title ##  \nbody") == "Title"
    assert markdown_files.strip_heading("# Title\n\nbody", "Title") == "body"


# API: an Idempotency-Key request hashes passwords before it takes the write lock


def test_creating_users_with_an_idempotency_key_hashes_first_and_replays(app, admin, db, monkeypatch):
    from bananawiki.wiki.features.api_service.api import users as users_api

    from .api_support import enable_api, issue

    enable_api(app, db)
    raw = issue(app, admin, ("users",))
    seen = []
    original = users_api.prehash_passwords
    monkeypatch.setattr(users_api.create_user, "_api_prehash_passwords",
                        lambda body: seen.append(body) or original(body))
    client = app.test_client()
    headers = {"Authorization": f"Bearer {raw}", "Idempotency-Key": "new-user-1"}
    body = {"username": "fresh_member", "password": "a long enough passphrase 42"}
    first = client.post("/api/v1/users", json=body, headers=headers)
    assert first.status_code == 201, first.get_data(as_text=True)
    assert seen, "the password was hashed before the transaction"
    again = client.post("/api/v1/users", json=body, headers=headers)
    assert again.status_code == 201 and again.headers.get("Idempotent-Replayed") == "true"
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'fresh_member'") == 1
    signed = app.test_client().post("/login", data={"username": "fresh_member",
                                                    "password": "a long enough passphrase 42"})
    assert signed.status_code == 302 and "/login" not in signed.headers["Location"]


# Jobs


def test_the_cron_command_respects_intervals_and_leases(app, db):
    scheduler = app.extensions["bananawiki.scheduler"]
    first = scheduler.run_due(command=True)
    assert "auth.prune_rate_limits" in first, "start-up delays do not apply to the command"
    assert "auth.prune_rate_limits" not in scheduler.run_due(command=True), "not due again yet"
    db.execute("UPDATE job_runs SET last_run_at = NULL, held_by = 'another-worker', lease_until = ? "
               "WHERE name = 'auth.prune_rate_limits'", (sql_in(minutes=10),))
    assert "auth.prune_rate_limits" not in scheduler.run_due(command=True), "another worker holds it"


def test_a_run_never_frees_a_lease_another_worker_took(app, db, monkeypatch):
    from bananawiki.wiki.features.auth import jobs

    class TakenOver:
        """The run outlived its lease and another worker claimed the job meanwhile."""

        def __init__(self, session):
            pass

        def prune(self, seconds):
            db.execute("UPDATE job_runs SET held_by = 'another-worker' WHERE name = 'auth.prune_rate_limits'")
            return 0

    monkeypatch.setattr(jobs, "SqlLimiter", TakenOver)
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="auth.prune_rate_limits")
    row = db.one("SELECT held_by, last_status FROM job_runs WHERE name = 'auth.prune_rate_limits'")
    assert row == {"held_by": "another-worker", "last_status": "ok"}


def test_long_ended_sessions_are_pruned(app, make_user, db):
    from bananawiki.wiki.features.auth import jobs

    user = make_user("old_timer")
    for row_id, revoked, expires in (("ancient", "2020-01-01 00:00:00", "2020-01-08 00:00:00"),
                                     ("recent", sql_in(days=-1), sql_in(days=6)),
                                     ("live", None, sql_in(days=6))):
        db.execute("INSERT INTO user_sessions (id, token_hash, user_id, created_at, last_seen_at, expires_at, "
                   "revoked_at) VALUES (?, ?, ?, '2020-01-01 00:00:00', '2020-01-01 00:00:00', ?, ?)",
                   (row_id, row_id, user["id"], expires, revoked))
    _run(app, jobs.prune_rate_limits)
    assert sorted(db.column("SELECT id FROM user_sessions WHERE user_id = ?", (user["id"],))) == ["live", "recent"]


def test_half_written_uploads_are_swept_after_a_day(app):
    from bananawiki.wiki import storage

    folder = app.config["BW"].folders.uploads
    os.makedirs(folder, exist_ok=True)
    old, fresh, kept = (os.path.join(folder, name) for name in (".upload-old", ".upload-fresh", "kept.png"))
    for path in (old, fresh, kept):
        with open(path, "wb") as handle:
            handle.write(b"x")
    os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    os.utime(kept, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    assert _run(app, storage.sweep_stale_temp_files) == 1
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(kept)


# Mail and database robustness


def test_a_mail_server_password_smtplib_cannot_send_is_a_failed_delivery(monkeypatch):
    import smtplib

    from bananawiki.core import mail

    class Refusing:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, user, password):
            password.encode("ascii")

        def send_message(self, message):
            raise AssertionError("not reached")

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    settings = mail.MailSettings(provider="smtp", sender="wiki@example.org", smtp_host="mail.example.org",
                                 smtp_port=587, smtp_security="starttls", smtp_username="wiki",
                                 smtp_password="pässword")
    ok, reason = mail.send(settings, mail.Message(to="someone@example.org", subject="Hi", text="Hello"))
    assert (ok, reason) == (False, "smtp_failed")


def test_a_failed_savepoint_after_sqlite_rolled_back_keeps_the_real_error(tmp_path):
    import sqlite3

    from bananawiki.core.sqlite import Session

    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    session = Session(conn)
    with pytest.raises(ZeroDivisionError), session.transaction(), session.transaction():
        conn.execute("ROLLBACK")  # what SQLite does by itself on SQLITE_FULL
        raise ZeroDivisionError
    assert not conn.in_transaction
