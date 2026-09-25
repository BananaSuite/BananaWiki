"""Security tests for the full-site export and import.

Admins are trusted with the whole wiki by design, so a plain admin may still
export everything or import a backup that rewrites any account. What these
tests pin down is the rest of that decision:

* export and import ask for the acting admin's password again, and both the
  successful and the refused attempts reach the wiki log;
* the migration page says the archive holds password hashes and that an
  import gives complete control;
* the merge mode ("keep") never changes an account that already exists, not
  even through a side table such as temp_roles or api_service__tokens;
* the documented move to a fresh installation still works end to end.
"""

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import config
import db
from helpers._passwords import generate_password_hash


@pytest.fixture(autouse=True)
def isolated_assets(tmp_path, monkeypatch):
    """Keep export and import away from the checkout's real asset folders."""
    for name in (
        "UPLOAD_FOLDER",
        "ATTACHMENT_FOLDER",
        "CHAT_ATTACHMENT_FOLDER",
        "KANBAN_ATTACHMENT_FOLDER",
        "CUSTOM_PAGE_FILES_FOLDER",
        "FAVICON_UPLOAD_FOLDER",
        "SITE_EXPORT_TEMP_DIR",
    ):
        monkeypatch.setattr(config, name, str(tmp_path / "assets" / name.lower()), raising=False)


@pytest.fixture
def logged_actions(monkeypatch):
    """Record every log_action call the migration routes make."""
    import routes.admin_migration as admin_mod

    calls = []
    original = admin_mod.log_action

    def _record(action, request, user=None, **details):
        calls.append((action, user["username"] if user else None, details))
        return original(action, request, user=user, **details)

    monkeypatch.setattr(admin_mod, "log_action", _record)
    return calls


@pytest.fixture
def owner_user(admin_user):
    """An owner with superuser status next to the plain admin from conftest."""
    uid = db.create_user("theowner", generate_password_hash("owner-pass"), role="owner")
    db.update_user(uid, is_superuser=1)
    return uid


def _zip_from_data(data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("site_export.json", json.dumps(data))
    return buf.getvalue()


def _import(client, data_or_zip, mode, password="admin123"):
    payload = data_or_zip if isinstance(data_or_zip, bytes) else _zip_from_data(data_or_zip)
    form = {"import_mode": mode, "import_file": (io.BytesIO(payload), "backup.zip")}
    if password is not None:
        form["password"] = password
    return client.post(
        "/admin/migration/import",
        data=form,
        content_type="multipart/form-data",
    )


def _takeover_export(admin_username="admin", owner_username="theowner"):
    """The auditor's file: the admin becomes owner and superuser, the owner a user."""
    data = db.export_site_data()
    for row in data["users"]:
        if row["username"] == admin_username:
            row["role"] = "owner"
            row["is_superuser"] = 1
        if row["username"] == owner_username:
            row["role"] = "user"
            row["is_superuser"] = 0
            row["password"] = generate_password_hash("pwned")
    return data


def _past():
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Password confirmation and logging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    [None, "", "wrong-password", "x" * 2000],
    ids=["missing", "empty", "wrong", "too-long"],
)
def test_export_refuses_without_the_admin_password(logged_in_admin, logged_actions, password):
    data = {} if password is None else {"password": password}
    resp = logged_in_admin.post("/admin/migration/export", data=data)
    assert resp.status_code == 302
    assert resp.content_type != "application/zip"
    assert resp.location.endswith("/admin/migration")
    page = logged_in_admin.get("/admin/migration").data
    assert b"Incorrect password" in page
    assert ("export_site_refused", "admin", {"reason": "wrong_password"}) in logged_actions
    assert not any(action == "export_site" for action, _, _ in logged_actions)


def test_export_with_the_admin_password_is_logged(logged_in_admin, logged_actions):
    resp = logged_in_admin.post("/admin/migration/export", data={"password": "admin123"})
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        exported = json.loads(zf.read("site_export.json"))
    # This is why the password is asked for: the hashes are in the file.
    assert any(row["username"] == "admin" and row["password"] for row in exported["users"])
    exports = [entry for entry in logged_actions if entry[0] == "export_site"]
    assert exports and exports[0][1] == "admin"


@pytest.mark.parametrize("password", [None, "wrong-password"], ids=["missing", "wrong"])
def test_takeover_import_is_refused_without_the_admin_password(
    logged_in_admin, admin_user, owner_user, logged_actions, password
):
    """Auditor's reproduction: without the password nothing changes."""
    resp = _import(logged_in_admin, _takeover_export(), "override", password=password)
    assert resp.status_code == 302
    assert resp.location.endswith("/admin/migration")
    me = db.get_user_by_id(admin_user)
    owner = db.get_user_by_id(owner_user)
    assert me["role"] == "admin" and not me["is_superuser"]
    assert owner["role"] == "owner" and owner["is_superuser"]
    assert ("import_site_refused", "admin", {"reason": "wrong_password"}) in logged_actions
    assert not any(action == "import_site" for action, _, _ in logged_actions)
    # The refusal happens before anything else: the session survives.
    assert logged_in_admin.get("/admin/migration").status_code == 200


def test_override_import_by_admin_is_full_control_but_confirmed_and_logged(
    logged_in_admin, admin_user, owner_user, logged_actions
):
    """Admins are trusted by design: with the password the import goes through."""
    resp = _import(logged_in_admin, _takeover_export(), "override")
    assert resp.status_code == 302
    assert resp.location.endswith("/login")
    assert db.get_user_by_id(admin_user)["role"] == "owner"
    assert db.get_user_by_id(owner_user)["role"] == "user"
    imports = [entry for entry in logged_actions if entry[0] == "import_site"]
    assert len(imports) == 1
    action, username, details = imports[0]
    assert username == "admin"
    assert details["mode"] == "override"
    assert details["accounts_in_file"] == 2


def test_impersonating_admin_cannot_export_or_import(client, admin_user, owner_user, logged_actions):
    """A superuser acting as another admin must stop impersonating first."""
    client.post("/login", data={"username": "theowner", "password": "owner-pass"})
    resp = client.post(f"/admin/users/{admin_user}/impersonate")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("impersonator_id") == owner_user

    resp = client.post("/admin/migration/export", data={"password": "admin123"})
    assert resp.content_type != "application/zip"
    resp = _import(client, _takeover_export(), "override")
    assert resp.location.endswith("/admin/migration")
    assert db.get_user_by_id(admin_user)["role"] == "admin"
    refusals = [entry for entry in logged_actions if entry[0].endswith("_refused")]
    assert [entry[2]["reason"] for entry in refusals] == ["impersonating", "impersonating"]


def test_migration_page_warns_about_hashes_and_full_control(logged_in_admin):
    body = logged_in_admin.get("/admin/migration").data.decode()
    # The page also embeds the translation table as JSON (with "<" escaped),
    # so these include the rendered markup to be sure the text is shown.
    assert "<strong>Keep this archive private.</strong> It contains the password hash of every account" in body
    assert "<strong>Importing a backup gives complete control of the wiki.</strong>" in body
    assert "not from an admin who imports a backup or installs a plugin" in body
    assert "Give the admin role only to people you trust with everything" in body
    # Both forms carry a password field.
    assert body.count('name="password"') == 2
    # The export never contained log files; the page no longer lists them.
    assert "<li>Log files</li>" not in body


# ---------------------------------------------------------------------------
# Merge mode ("keep") never changes existing accounts
# ---------------------------------------------------------------------------


def test_keep_mode_cannot_raise_or_demote_existing_accounts(admin_user, owner_user):
    data = _takeover_export()
    skipped = db.import_site_data(data, "keep")
    me = db.get_user_by_id(admin_user)
    owner = db.get_user_by_id(owner_user)
    assert me["role"] == "admin" and not me["is_superuser"]
    assert owner["role"] == "owner" and owner["is_superuser"]
    assert skipped["users"] == 2


def test_keep_mode_ignores_account_that_clashes_by_username_only(admin_user, owner_user):
    """A file account with a new id but an existing name is the same account."""
    data = db.export_site_data()
    for row in data["users"]:
        if row["username"] == "admin":
            row["id"] = "not-the-local-id"
            row["username"] = "ADMIN"
            row["role"] = "owner"
            row["is_superuser"] = 1
    data["temp_roles"] = [{
        "id": 900, "user_id": "not-the-local-id", "original_role": "owner",
        "expires_at": _past(), "show_countdown": 1, "set_by": None,
        "created_at": _past(), "updated_at": _past(),
    }]
    skipped = db.import_site_data(data, "keep")
    assert db.get_user_by_id("not-the-local-id") is None
    assert db.get_role_expiry("not-the-local-id") is None
    assert db.get_user_by_id(admin_user)["role"] == "admin"
    # Both file accounts clash (the owner keeps its id), and the schedule of the
    # renamed one is dropped with it.
    assert skipped == {"users": 2, "temp_roles": 1}


def test_keep_mode_skips_role_schedules_for_existing_accounts(admin_user, owner_user):
    """temp_roles would change the role later, when the schedule expires."""
    data = db.export_site_data()
    data["temp_roles"] = [
        # Demote the owner to a plain user once the schedule "expires".
        {"id": 901, "user_id": owner_user, "original_role": "user", "expires_at": _past(),
         "show_countdown": 1, "set_by": None, "created_at": _past(), "updated_at": _past()},
        # Raise the admin to owner the same way.
        {"id": 902, "user_id": admin_user, "original_role": "owner", "expires_at": _past(),
         "show_countdown": 1, "set_by": None, "created_at": _past(), "updated_at": _past()},
    ]
    skipped = db.import_site_data(data, "keep")
    db.cleanup_expired_temp_roles()
    assert db.get_user_by_id(owner_user)["role"] == "owner"
    assert db.get_user_by_id(admin_user)["role"] == "admin"
    assert skipped["temp_roles"] == 2


def test_keep_mode_skips_deletion_schedules_for_existing_accounts(admin_user, owner_user):
    superadmin = db.create_user("superadmin", generate_password_hash("pw-super-1"), role="admin")
    db.update_user(superadmin, is_superuser=1)
    data = db.export_site_data()
    data["temp_users"] = [{
        "id": 903, "user_id": superadmin, "expires_at": _past(), "show_countdown": 1,
        "set_by": None, "created_at": _past(), "updated_at": _past(),
    }]
    skipped = db.import_site_data(data, "keep")
    db.cleanup_expired_temp_users()
    assert db.get_user_by_id(superadmin) is not None
    assert skipped["temp_users"] == 1


def test_keep_mode_does_not_add_sign_in_tokens_to_existing_accounts(admin_user, owner_user):
    """A token hash the importer knows the secret for must not land on the owner."""
    helper = db.create_user("helper", generate_password_hash("pw-helper-1"), role="user")
    raw = db.create_api_token(helper, name="mine")
    with db.get_db_context() as conn:
        token_row = dict(conn.execute(
            "SELECT * FROM api_service__tokens WHERE user_id=?", (helper,)
        ).fetchone())
        conn.execute("DELETE FROM api_service__tokens WHERE user_id=?", (helper,))
        conn.commit()
    assert db.verify_api_service_token(raw) == (None, None)

    data = db.export_site_data()
    forged = dict(token_row, user_id=owner_user, id=9999)
    data["api_service__tokens"] = [forged]
    data["api_tokens"] = [{"id": 9998, "user_id": owner_user, "token_hash": "x", "created_at": _past()}]
    data["userbot_api_tokens"] = [{"id": 9997, "user_id": owner_user, "token_hash": "y",
                                   "created_at": _past(), "last_used_at": None}]
    skipped = db.import_site_data(data, "keep")
    assert db.verify_api_service_token(raw) == (None, None)
    assert skipped["api_service__tokens"] == 1
    assert skipped["api_tokens"] == 1
    assert skipped["userbot_api_tokens"] == 1


def test_keep_mode_skips_merge_requests_naming_existing_accounts(admin_user, owner_user):
    data = db.export_site_data()
    data["account_merge_requests"] = [{
        "id": 77, "source_user_id": owner_user, "target_user_id": admin_user,
        "created_by": admin_user, "status": "approved_by_both",
        "source_approved": 1, "target_approved": 1, "admin_approved": None,
        "request_reason": "", "completed_at": None, "created_at": _past(),
    }]
    skipped = db.import_site_data(data, "keep")
    with db.get_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_merge_requests").fetchone()[0] == 0
    assert skipped["account_merge_requests"] == 1


def _upgraded_owner():
    """An owner as an upgraded installation stores it: the old integer id as text."""
    with db.get_db_context() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password, role, is_superuser) "
            "VALUES ('1', 'firstowner', ?, 'owner', 1)",
            (generate_password_hash("first-owner-pass"),),
        )
        conn.commit()
    return "1"


@pytest.mark.parametrize(
    "forged_id",
    [True, 1, {"__b64__": base64.b64encode(b"1").decode("ascii")}],
    ids=["json-true", "json-number", "blob"],
)
def test_keep_mode_compares_ids_the_way_sqlite_stores_them(admin_user, forged_id):
    """JSON true is stored as '1', which is the owner's id after an upgrade."""
    owner = _upgraded_owner()
    helper = db.create_user("helper", generate_password_hash("pw-helper-1"), role="user")
    raw = db.create_api_token(helper, name="mine")
    with db.get_db_context() as conn:
        token_row = dict(conn.execute(
            "SELECT * FROM api_service__tokens WHERE user_id=?", (helper,)
        ).fetchone())
        conn.execute("DELETE FROM api_service__tokens WHERE user_id=?", (helper,))
        conn.commit()

    data = db.export_site_data()
    data["users"].append({
        "id": forged_id, "username": "not-taken", "password": generate_password_hash("pw-x-1"),
        "role": "owner", "is_superuser": 1, "created_at": _past(),
    })
    data["temp_roles"] = [{
        "id": 906, "user_id": forged_id, "original_role": "user", "expires_at": _past(),
        "show_countdown": 1, "set_by": None, "created_at": _past(), "updated_at": _past(),
    }]
    data["api_service__tokens"] = [dict(token_row, user_id=forged_id, id=9996)]
    skipped = db.import_site_data(data, "keep")
    db.cleanup_expired_temp_roles()

    row = db.get_user_by_id(owner)
    assert row["username"] == "firstowner"
    assert row["role"] == "owner" and row["is_superuser"] == 1
    assert db.verify_api_service_token(raw) == (None, None)
    assert skipped["temp_roles"] == 1
    assert skipped["api_service__tokens"] == 1


def test_keep_mode_still_adds_new_accounts_with_their_rows(admin_user, owner_user):
    """Legitimate merges keep working: accounts that are new here come in whole."""
    data = db.export_site_data()
    data["users"].append({
        "id": "newcomer-id", "username": "newcomer", "password": generate_password_hash("pw-new-1"),
        "role": "editor", "suspended": 0, "is_superuser": 0, "created_at": _past(),
    })
    data["temp_roles"] = [{
        "id": 904, "user_id": "newcomer-id", "original_role": "user",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        "show_countdown": 1, "set_by": None, "created_at": _past(), "updated_at": _past(),
    }]
    skipped = db.import_site_data(data, "keep")
    assert db.get_user_by_username("newcomer")["role"] == "editor"
    assert db.get_role_expiry("newcomer-id") is not None
    assert "temp_roles" not in skipped


def test_keep_mode_route_reports_left_out_rows(logged_in_admin, admin_user, owner_user, logged_actions):
    data = _takeover_export()
    data["temp_roles"] = [{
        "id": 905, "user_id": owner_user, "original_role": "user", "expires_at": _past(),
        "show_countdown": 1, "set_by": None, "created_at": _past(), "updated_at": _past(),
    }]
    resp = _import(logged_in_admin, data, "keep")
    assert resp.location.endswith("/login")
    page = logged_in_admin.get("/login").data
    assert b"imported successfully" in page
    assert b"would have changed accounts" in page
    imports = [entry for entry in logged_actions if entry[0] == "import_site"]
    assert imports[0][2]["existing_accounts_kept"] == 2
    assert imports[0][2]["account_rows_skipped"] == 1
    assert db.get_user_by_id(owner_user)["role"] == "owner"
    assert db.get_user_by_id(admin_user)["role"] == "admin"


def test_replacement_modes_do_not_filter_rows(admin_user, owner_user):
    """override and delete_all hand over control on purpose (see the page warning)."""
    data = db.export_site_data()
    assert db.import_site_data(data, "override") == {}
    assert db.import_site_data(data, "delete_all") == {}


# ---------------------------------------------------------------------------
# Moving a wiki to a fresh installation
# ---------------------------------------------------------------------------


def _export_old_wiki(client):
    """Build the old wiki in the current DB and download its export."""
    old_owner = db.create_user("alice", generate_password_hash("old-owner-pass"), role="owner")
    db.update_user(old_owner, is_superuser=1)
    bob = db.create_user("bob", generate_password_hash("bob-old-pass"), role="editor")
    db.update_site_settings(setup_done=1, site_name="Old Wiki")
    db.create_page("Moved page", "moved-page", "Carried over", user_id=old_owner)
    token = db.create_api_token(bob, name="ci")
    client.post("/login", data={"username": "alice", "password": "old-owner-pass"})
    resp = client.post("/admin/migration/export", data={"password": "old-owner-pass"})
    assert resp.status_code == 200 and resp.content_type == "application/zip"
    client.post("/logout")
    return old_owner, bob, token, resp.data


def _fresh_wiki_with_setup_account(client, monkeypatch, tmp_path, username):
    """Point the app at an empty database and claim it through /setup."""
    from app import app

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "fresh.db"))
    db.init_db()
    assert not db.get_site_settings()["setup_done"]
    monkeypatch.setitem(app.config, "ENFORCE_SETUP_TOKEN", True)
    client.post("/logout")
    resp = client.post("/setup", data={
        "setup_token": config.SETUP_TOKEN,
        "username": username,
        "password": "setup-pass-123",
        "confirm_password": "setup-pass-123",
    })
    assert resp.status_code == 302
    setup_account = db.get_user_by_username(username)
    assert setup_account["role"] == "owner"
    # The operator finishes the forced onboarding before reaching the admin pages.
    with db.get_db_context() as conn:
        conn.execute("UPDATE users SET onboarding_required=0 WHERE id=?", (setup_account["id"],))
        conn.commit()
    client.post("/login", data={"username": username, "password": "setup-pass-123"})
    assert client.get("/admin/migration").status_code == 200
    return setup_account["id"]


def _login_works(client, username, password):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": password})
    with client.session_transaction() as sess:
        signed_in = bool(sess.get("user_id"))
    client.post("/logout")
    return signed_in


def test_move_to_fresh_wiki_override_replaces_setup_account(client, monkeypatch, tmp_path):
    """The documented move: the setup account imports the export in override mode."""
    old_owner, bob, token, archive = _export_old_wiki(client)
    setup_id = _fresh_wiki_with_setup_account(client, monkeypatch, tmp_path, "alice")
    assert setup_id != old_owner

    resp = client.post(
        "/admin/migration/import",
        data={
            "import_mode": "override",
            "password": "setup-pass-123",
            "import_file": (io.BytesIO(archive), "site_export.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302 and resp.location.endswith("/login")

    # The exported account with the same name replaced the setup account.
    assert db.get_user_by_id(setup_id) is None
    alice = db.get_user_by_username("alice")
    assert alice["id"] == old_owner
    assert alice["role"] == "owner" and alice["is_superuser"] == 1
    assert db.get_user_by_id(bob)["role"] == "editor"
    assert db.get_page_by_slug("moved-page")["content"] == "Carried over"
    assert db.get_site_settings()["site_name"] == "Old Wiki"
    # API tokens move with their accounts; the merge-mode filter does not apply here.
    token_row, user_row = db.verify_api_service_token(token)
    assert user_row["username"] == "bob"

    assert _login_works(client, "alice", "old-owner-pass")
    assert not _login_works(client, "alice", "setup-pass-123")
    assert _login_works(client, "bob", "bob-old-pass")


def test_move_to_fresh_wiki_override_keeps_differently_named_setup_account(client, monkeypatch, tmp_path):
    """With another name the setup account stays next to the moved accounts."""
    old_owner, bob, _token, archive = _export_old_wiki(client)
    setup_id = _fresh_wiki_with_setup_account(client, monkeypatch, tmp_path, "tempadmin")
    resp = client.post(
        "/admin/migration/import",
        data={
            "import_mode": "override",
            "password": "setup-pass-123",
            "import_file": (io.BytesIO(archive), "site_export.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.location.endswith("/login")
    assert db.get_user_by_id(setup_id)["role"] == "owner"
    assert db.get_user_by_id(old_owner)["role"] == "owner"
    assert _login_works(client, "alice", "old-owner-pass")
    assert _login_works(client, "bob", "bob-old-pass")

    # docs/server-migration.md: other admins cannot delete an owner, not even
    # the imported superuser, so the temporary account deletes itself.
    client.post("/login", data={"username": "alice", "password": "old-owner-pass"})
    client.post(f"/admin/users/{setup_id}/edit", data={"action": "delete"})
    assert db.get_user_by_id(setup_id) is not None
    client.post("/logout")
    client.post("/login", data={"username": "tempadmin", "password": "setup-pass-123"})
    client.post("/settings", data={"action": "delete_account", "password": "setup-pass-123"})
    assert db.get_user_by_id(setup_id) is None
    assert {u["username"] for u in db.list_users()} == {"alice", "bob"}


def test_move_to_fresh_wiki_delete_all_leaves_only_exported_accounts(client, monkeypatch, tmp_path):
    old_owner, bob, _token, archive = _export_old_wiki(client)
    setup_id = _fresh_wiki_with_setup_account(client, monkeypatch, tmp_path, "tempadmin")
    resp = client.post(
        "/admin/migration/import",
        data={
            "import_mode": "delete_all",
            "password": "setup-pass-123",
            "import_file": (io.BytesIO(archive), "site_export.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.location.endswith("/login")
    assert db.get_user_by_id(setup_id) is None
    assert {u["username"] for u in db.list_users()} == {"alice", "bob"}
    assert _login_works(client, "alice", "old-owner-pass")
