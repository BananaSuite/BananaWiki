"""The account, content and operations flows end to end, one test per flow.

Each test drives the flow the way a person does (the HTTP test client; the
lifecycle and hosting runtime APIs for the operations flows) and asserts the
outcome that matters for security: who can get in, what ends at once, what
leaves the wiki and what is really gone.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from bananawiki.core.timeutil import sql_in, utcnow
from tests.conftest import PASSWORD
from tests.features.api_support import call, enable_api, issue
from tests.features.chat_support import send, start_dm
from tests.features.governance_support import build_app
from tests.features.people_support import image_bytes, make_page
from tests.features.plugins_support import HELLO_PLUGIN, build, install_folder, row, set_row

NEW_PASSWORD = "an entirely new secret"


@pytest.fixture
def no_bots(db):
    db.execute("UPDATE site_settings SET bot_protection_enabled = 0")


def _signup(client, username, invite=None):
    data = {"username": username, "password": PASSWORD, "confirm_password": PASSWORD}
    if invite is not None:
        data["invite_code"] = invite
    return client.post("/signup", data=data)


def _signed_in(client) -> bool:
    """Whether *client* reaches a private page (not sent to sign-in or the account status page)."""
    return client.get("/_probe/private").status_code == 200


def _session_cookie(client) -> str:
    cookie = client.get_cookie("bw_session")
    assert cookie is not None
    return cookie.value


def _with_cookie(app, value: str):
    other = app.test_client()
    other.set_cookie("bw_session", value)
    return other


# Sign-up


def test_open_signup_honours_its_end_date(app, client, db, no_bots):
    db.execute("UPDATE site_settings SET open_signup = 1, open_signup_until = ?", (sql_in(minutes=-1),))
    assert _signup(client, "too_late").status_code == 400
    db.execute("UPDATE site_settings SET open_signup_until = ?", (sql_in(days=1),))
    assert _signup(client, "in_time").status_code == 302
    assert db.column("SELECT username FROM users") == ["in_time"]
    new = db.one("SELECT role, approval_status FROM users WHERE username = 'in_time'")
    assert new == {"role": "user", "approval_status": "approved"}
    assert client.post("/login", data={"username": "in_time", "password": PASSWORD}).status_code == 302
    assert _signed_in(client)


def test_signup_with_approval_is_blocked_until_an_administrator_approves(app, client, db, no_bots, admin):
    db.execute("UPDATE site_settings SET open_signup = 1, open_signup_until = NULL, approval_required = 1")
    assert _signup(client, "waiting").status_code == 302
    pending = db.one("SELECT id, approval_status FROM users WHERE username = 'waiting'")
    assert pending["approval_status"] == "pending"
    client.post("/login", data={"username": "waiting", "password": PASSWORD})
    assert client.get("/_probe/private").headers["Location"].endswith("/account-status")
    assert client.get("/settings").status_code == 302

    boss = app.test_client()
    boss.post("/login", data={"username": admin["username"], "password": PASSWORD})
    boss.post(f"/admin/users/{pending['id']}/edit", data={"action": "approve_user"})

    assert db.scalar("SELECT approval_status FROM users WHERE id = ?", (pending["id"],)) == "approved"
    assert _signed_in(client), "the waiting session goes on once approved"


def test_invite_codes_are_single_use_and_give_their_role(admin_client, db, no_bots, app):
    admin_client.post("/admin/codes/generate", data={"max_uses": "1", "expiry_mode": "48h",
                                                     "assigned_role": "editor"})
    code = db.scalar("SELECT code FROM invite_codes")
    admin_client.post("/logout")

    assert _signup(admin_client, "closed_door").status_code == 400, "sign-up is closed without a code"
    assert _signup(admin_client, "first_in", invite=code.lower()).status_code == 302
    assert db.scalar("SELECT role FROM users WHERE username = 'first_in'") == "editor"
    second = app.test_client()
    assert _signup(second, "second_in", invite=code).status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username IN ('closed_door', 'second_in')") == 0
    assert db.scalar("SELECT use_count FROM invite_codes") == 1


# Signing in and out, sessions


def test_login_and_logout_end_the_session_on_the_server(app, client, make_user, login):
    bob = make_user("bob")
    assert client.post("/login", data={"username": "bob", "password": "wrong password"}).status_code == 401
    assert not _signed_in(client)
    login(client, bob)
    assert _signed_in(client)
    stolen = _session_cookie(client)
    assert _signed_in(_with_cookie(app, stolen))

    client.post("/logout")
    assert not _signed_in(client)
    assert not _signed_in(_with_cookie(app, stolen)), "a copied cookie dies with the session"


def test_session_list_revoke_one_and_sign_out_everywhere(app, make_user, login, db):
    bob, eve = make_user("bob"), make_user("eve")
    here, laptop, phone, eves = (app.test_client() for _ in range(4))
    ids: list[str] = []
    for device in (here, laptop, phone):
        known = set(ids)
        login(device, bob)
        current = db.column("SELECT id FROM user_sessions WHERE user_id = ?", (bob["id"],))
        ids += [session_id for session_id in current if session_id not in known]
    assert len(ids) == 3
    login(eves, eve)
    eve_session = db.scalar("SELECT id FROM user_sessions WHERE user_id = ?", (eve["id"],))
    page = here.get("/settings/security").get_data(as_text=True)
    assert all(session_id in page for session_id in ids[1:]) and eve_session not in page

    assert here.post(f"/settings/sessions/{eve_session}/revoke").status_code == 404
    assert _signed_in(eves)
    here.post(f"/settings/sessions/{ids[1]}/revoke")
    assert not _signed_in(laptop) and _signed_in(phone) and _signed_in(here)

    assert here.post("/settings/sessions/logout-all").headers["Location"].endswith("/login")
    assert not _signed_in(here) and not _signed_in(phone)
    assert _signed_in(eves), "other accounts keep their sessions"


def test_password_change_ends_the_other_sessions(app, make_user, login):
    bob = make_user("bob")
    here, other = app.test_client(), app.test_client()
    login(here, bob)
    login(other, bob)
    stolen = _session_cookie(other)
    response = here.post("/settings/password", data={
        "current_password": PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert response.status_code == 302
    assert _signed_in(here)
    assert not _signed_in(other) and not _signed_in(_with_cookie(app, stolen))
    fresh = app.test_client()
    assert fresh.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401
    login(fresh, bob, password=NEW_PASSWORD)


def test_admin_password_reset_signs_the_account_out_and_forces_a_new_password(app, admin_client, make_user,
                                                                              login):
    bob = make_user("bob")
    bobs = app.test_client()
    login(bobs, bob)
    admin_client.post(f"/admin/users/{bob['id']}/edit", data={
        "action": "change_password", "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
        "force_password_change": "1"})
    assert not _signed_in(bobs)
    assert bobs.post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401
    login(bobs, bob, password=NEW_PASSWORD)
    assert bobs.get("/_probe/private").headers["Location"].endswith("/force-change-password")
    assert _signed_in(admin_client), "the administrator stays signed in"


# Suspension


def test_suspension_cuts_sessions_tokens_userbot_key_and_chat_at_once(app, admin_client, make_user, login,
                                                                       db):
    enable_api(app, db)
    bob = make_user("bob", api_access_enabled=1, userbot_enabled=1)
    make_user("eve")
    token = issue(app, bob, ["pages"])
    userbot_key = issue(app, bob, ["userbot"], name="userbot")
    bobs = app.test_client()
    login(bobs, bob)
    chat = start_dm(bobs, "eve")
    assert send(bobs, f"/chats/{chat}/send", "before").json["ok"]
    assert call(bobs, "GET", "/pages", token).status_code == 200
    assert call(bobs, "GET", "/userbot/me", userbot_key).status_code == 200

    admin_client.post(f"/admin/users/{bob['id']}/edit", data={"action": "suspend", "suspend_reason": "spam"})

    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (bob["id"],)) == 1
    assert not _signed_in(bobs)
    assert send(bobs, f"/chats/{chat}/send", "after").status_code != 200
    assert call(bobs, "GET", "/pages", token).status_code in (401, 403)
    assert call(bobs, "GET", "/userbot/me", userbot_key).status_code in (401, 403)
    # Signing in again reaches the status page only, and chat still refuses.
    fresh = app.test_client()
    fresh.post("/login", data={"username": "bob", "password": PASSWORD})
    assert not _signed_in(fresh)
    assert send(fresh, f"/chats/{chat}/send", "again").status_code != 200
    assert db.column("SELECT content FROM chat_messages WHERE chat_id = ?", (chat,)) == ["before"]


# Data export


def _everything(archive: zipfile.ZipFile) -> bytes:
    return b"".join(archive.read(name) for name in archive.namelist())


def test_data_export_holds_own_data_only_and_no_secrets(app, make_user, login, db):
    enable_api(app, db)
    bob = make_user("bob", role="editor", api_access_enabled=1)
    eve = make_user("eve", role="editor")
    issue(app, bob, ["pages"])
    make_page(app, "Bob's page", "bob wrote this", author_id=bob["id"])
    make_page(app, "Eve's page", "eve wrote this", author_id=eve["id"])
    bobs, eves = app.test_client(), app.test_client()
    login(bobs, bob)
    login(eves, eve)
    chat = start_dm(bobs, "eve")
    assert send(bobs, f"/chats/{chat}/send", "bob says hi").json["ok"]
    assert send(eves, f"/chats/{chat}/send", "eve answers privately").json["ok"]
    # Two-step verification turned on (after signing in, which it would otherwise interrupt).
    db.execute("INSERT INTO auth__two_step (user_id, secret_encrypted, recovery_hashes, last_counter, "
               "enabled_at) VALUES (?, 'encrypted-authenticator-key', '[\"recovery-digest-1\"]', 7, "
               "'2026-03-04 05:06:07')", (bob["id"],))

    response = bobs.get("/settings/export")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    everything = _everything(archive)
    assert json.loads(archive.read("account.json"))["username"] == "bob"
    assert b"bob wrote this" in everything and b"bob says hi" in everything

    secrets = [db.scalar("SELECT password FROM users WHERE id = ?", (bob["id"],)),
               *db.column("SELECT token_hash FROM api_service__tokens"),
               *db.column("SELECT token_hash FROM user_sessions"),
               "encrypted-authenticator-key", "recovery-digest-1",
               "eve wrote this", "eve answers privately",
               db.scalar("SELECT password FROM users WHERE id = ?", (eve["id"],))]
    for secret in secrets:
        assert secret.encode() not in everything, secret
    assert bobs.get(f"/admin/users/{eve['id']}/export").status_code == 403


# Account deletion


def _upload_pictures(client) -> None:
    client.post("/settings/profile", content_type="multipart/form-data",
                data={"real_name": "Bob", "avatar": (io.BytesIO(image_bytes("JPEG")), "me.jpg")})
    client.post("/settings/display/background", content_type="multipart/form-data",
                data={"background": (io.BytesIO(image_bytes("JPEG")), "bg.jpg")})


def _uploads(app) -> set[Path]:
    return {path for path in Path(app.config["BW"].folders.uploads).rglob("*") if path.is_file()}


ACCOUNT_TABLES = {"users": "id", "user_sessions": "user_id", "api_service__tokens": "user_id",
                  "user_profiles": "user_id", "drafts": "user_id"}


def _account_rows(db, user_id: str) -> dict[str, int]:
    return {table: db.scalar(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,))
            for table, column in ACCOUNT_TABLES.items()}


def _populate(app, client, db, user) -> str:
    """Give *user* a profile picture, a background, an API token, a draft and a direct message."""
    enable_api(app, db)
    raw = issue(app, user, ["pages"])
    _upload_pictures(client)
    page = make_page(app, "Shared", "x")
    db.execute("INSERT INTO drafts (page_id, user_id, content, updated_at) "
               "VALUES (?, ?, 'private draft', ?)", (page["id"], user["id"], sql_in(minutes=0)))
    chat = start_dm(client, "eve")
    assert send(client, f"/chats/{chat}/send", "a word before leaving").json["ok"]
    assert all(_account_rows(db, user["id"]).values())
    return raw


def test_account_deletion_removes_sessions_tokens_pictures_and_private_content(app, make_user, login, db):
    bob = make_user("bob", api_access_enabled=1)
    make_user("eve")
    bobs = app.test_client()
    login(bobs, bob)
    token = _populate(app, bobs, db, bob)
    pictures = _uploads(app)
    assert len(pictures) == 2
    stolen = _session_cookie(bobs)

    bobs.post("/settings/delete", data={"password": "wrong password"})
    assert _account_rows(db, bob["id"])["users"] == 1
    assert bobs.post("/settings/delete", data={"password": PASSWORD}).headers["Location"].endswith("/login")

    assert _account_rows(db, bob["id"]) == dict.fromkeys(ACCOUNT_TABLES, 0)
    assert not any(path.exists() for path in pictures)
    assert not _signed_in(_with_cookie(app, stolen))
    assert call(app.test_client(), "GET", "/pages", token).status_code == 401
    assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE sender_id = ?", (bob["id"],)) == 0
    assert app.test_client().post("/login", data={"username": "bob", "password": PASSWORD}).status_code == 401


def test_scheduled_account_deletion_waits_for_its_date_then_removes_everything(app_factory, make_user,
                                                                               login, db):
    app = build_app(app_factory, "temporary_accounts")  # the same database as ``db``
    boss, bob = make_user("boss", role="admin"), make_user("bob", api_access_enabled=1)
    make_user("eve")
    bobs, bosses = app.test_client(), app.test_client()
    login(bobs, bob)
    token = _populate(app, bobs, db, bob)
    pictures = _uploads(app)
    assert len(pictures) == 2
    login(bosses, boss)
    when = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    bosses.post("/admin/temporary/user", data={"username": "bob", "expires_at": when})
    assert db.scalar("SELECT COUNT(*) FROM temp_users WHERE user_id = ?", (bob["id"],)) == 1
    scheduler = app.extensions["bananawiki.scheduler"]

    scheduler.run_due(force=True, only="temporary_accounts.expire")
    assert _signed_in(bobs), "nothing happens before the date"
    assert call(app.test_client(), "GET", "/pages", token).status_code == 200
    assert all(path.exists() for path in pictures)

    db.execute("UPDATE temp_users SET expires_at = ? WHERE user_id = ?", (sql_in(minutes=-1), bob["id"]))
    scheduler.run_due(force=True, only="temporary_accounts.expire")
    assert _account_rows(db, bob["id"]) == dict.fromkeys(ACCOUNT_TABLES, 0)
    assert not _signed_in(bobs)
    assert call(app.test_client(), "GET", "/pages", token).status_code == 401
    assert not any(path.exists() for path in pictures)


# Page deletion grace period


def test_page_deletion_grace_period_restore_within_purge_after(app_factory, make_user, login, db):
    app = build_app(app_factory, "deletion_slowdown")  # the same database as ``db``
    boss, reader = make_user("boss", role="admin"), make_user("reader")
    page = make_page(app, "Doomed", "doomed words")
    bosses, readers = app.test_client(), app.test_client()
    login(bosses, boss)
    login(readers, reader)
    url = f"/page/{page['slug']}"
    assert readers.get(url).status_code == 200

    bosses.post(f"{url}/delete")
    assert readers.get(url).status_code == 404, "hidden while it waits"
    assert bosses.post(f"/admin/pending-deletions/{page['id']}/restore").status_code == 302
    assert readers.get(url).status_code == 200

    bosses.post(f"{url}/delete")
    scheduler = app.extensions["bananawiki.scheduler"]
    scheduler.run_due(force=True, only="deletion_slowdown.purge")
    assert db.scalar("SELECT pending_deletion FROM pages WHERE id = ?", (page["id"],)) == 1, "kept within"
    db.execute("UPDATE pages SET pending_deletion_at = ? WHERE id = ?", (sql_in(hours=-49), page["id"]))
    scheduler.run_due(force=True, only="deletion_slowdown.purge")
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE id = ?", (page["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == 0
    assert bosses.post(f"/admin/pending-deletions/{page['id']}/restore").status_code == 404
    assert readers.get(url).status_code == 404


# Third-party plugins


def test_external_plugins_load_only_when_allowed_and_enabled(app_factory, tmp_path, make_user, login):
    install_folder(tmp_path, HELLO_PLUGIN)
    first = build(app_factory, tmp_path)
    assert row(first, "hello_plugin")["enabled"] == 0, "a new folder is registered switched off"
    assert "hello_plugin" not in first.extensions["bananawiki.registry"].features

    set_row(first, "hello_plugin", True)
    refused = build(app_factory, tmp_path, BW_ALLOW_EXTERNAL_PLUGINS="0")
    assert "hello_plugin" not in refused.extensions["bananawiki.registry"].features
    unisolated = build(app_factory, tmp_path, BW_MANAGED_HOSTING="1")
    assert "hello_plugin" not in unisolated.extensions["bananawiki.registry"].features

    allowed = build(app_factory, tmp_path)
    client = allowed.test_client()
    assert client.get("/hello").status_code == 302, "plugin views are private by default"
    login(client, make_user("reader"))
    assert client.get("/hello").status_code == 200
    blocked = refused.test_client()
    login(blocked, {"username": "reader"})
    assert blocked.get("/hello").status_code == 404


# Hosting: tenant isolation


def test_a_tenant_cannot_read_another_tenants_files_through_links(tmp_path):
    from bananawiki.hosting.runtime import RuntimeFailure, tenantfs
    from tests.hosting.agent_fakes import make_runtime, make_spec, provision

    runtime, _agent = make_runtime(tmp_path)
    acme, beta = make_spec("acme"), make_spec("beta", port=6002)
    provision(runtime, acme)
    provision(runtime, beta)
    base = Path(runtime._cfg().instances_dir)
    secret = base / "acme" / "storage" / "uploads" / "secret.png"
    secret.write_bytes(b"ACME-PRIVATE-BYTES")
    (base / "acme" / "storage" / "attachments" / "contract.pdf").write_bytes(b"ACME-PRIVATE-BYTES")
    # What beta's own code (a plugin) can plant in its data directory.
    beta_root = base / "beta"
    (beta_root / "storage" / "uploads" / "steal.png").symlink_to(secret)
    escape = "../../../acme/storage/uploads/secret.png"
    (beta_root / "storage" / "uploads" / "relative.png").symlink_to(escape)
    (beta_root / "storage" / "attachments").rename(beta_root / "storage" / "attachments-old")
    (beta_root / "storage" / "attachments").symlink_to(base / "acme" / "storage" / "attachments")

    for relative in ("storage/uploads/steal.png", "storage/uploads/relative.png",
                     "storage/attachments/contract.pdf", f"storage/uploads/{escape}"):
        with pytest.raises((RuntimeFailure, OSError)):
            tenantfs.read_bytes(beta_root, relative, max_bytes=1024)
        with pytest.raises((RuntimeFailure, OSError)):
            tenantfs.copy_out(beta_root, relative, tmp_path / "copied")
        assert not (tmp_path / "copied").exists()
    with pytest.raises(RuntimeFailure):
        tenantfs.tenant_path(base, "../acme")

    (beta_root / "storage" / "uploads" / "own.png").write_bytes(b"BETA-OWN-BYTES")
    out = tmp_path / "out"
    out.mkdir()
    with zipfile.ZipFile(runtime.export_archive(beta, out)) as exported:
        everything = b"".join(exported.read(name) for name in exported.namelist())
    assert b"BETA-OWN-BYTES" in everything and b"ACME-PRIVATE-BYTES" not in everything
    assert secret.read_bytes() == b"ACME-PRIVATE-BYTES"


# Operations: backup, restore and update rollback


def _titles(root: Path) -> list[str]:
    connection = sqlite3.connect(root / "data/bananawiki.db")
    try:
        return [row[0] for row in connection.execute("SELECT title FROM pages ORDER BY id")]
    finally:
        connection.close()


def _add_page(root: Path, title: str) -> None:
    connection = sqlite3.connect(root / "data/bananawiki.db")
    connection.execute("INSERT INTO pages (title) VALUES (?)", (title,))
    connection.commit()
    connection.close()


def test_backup_restore_and_failed_update_rollback_round_trip(tmp_path):
    from bananawiki.ops.files import read_json
    from bananawiki.ops.manager import Manager
    from tests.ops.ops_fakes import FakeSystem, Upstream, legacy_install

    system, upstream = FakeSystem(tmp_path / "host"), Upstream(tmp_path)
    root = tmp_path / "opt" / "bananawiki"
    release = upstream.commit("1.4 release", modern=False)
    legacy_install(root, system, upstream, release)
    manager = Manager(root, system=system)

    _add_page(root, "kept")
    package = manager.backup()
    _add_page(root, "after the backup")
    (root / "data/uploads/picture.png").unlink()
    assert manager.restore(package)["outcome"] == "complete"
    assert _titles(root) == ["Home", "kept"]
    assert (root / "data/uploads/picture.png").is_file()

    bad = upstream.commit("broken release")
    system.unhealthy_revisions.add(bad)

    def damage(_names):
        if (root / "current").resolve().name == bad:
            _add_page(root, "half-migrated")

    system.on_start.append(damage)
    with pytest.raises(RuntimeError):
        manager.update()
    assert (root / "current").resolve().name == release
    assert _titles(root) == ["Home", "kept"]
    assert read_json(root / "config/status.json")["outcome"] == "rolled_back"
    assert not os.path.exists(root / "config/transaction.json")
