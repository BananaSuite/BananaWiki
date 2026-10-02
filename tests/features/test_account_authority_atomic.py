"""Account authority must survive changes between verification and a write."""

from contextlib import contextmanager

import pytest
from conftest import PASSWORD

from bananawiki.core import passwords
from bananawiki.wiki import accounts, auth
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.db import db as wiki_db
from bananawiki.wiki.features.admin import service as administration
from bananawiki.wiki.features.auth import signin
from bananawiki.wiki.features.users import service as profiles


@pytest.mark.parametrize("endpoint", ["/login", "/admin/sign-in", "/session-conflict/force"])
@pytest.mark.parametrize("change", ["password_reset", "deletion"])
def test_sign_in_cannot_recreate_a_session_after_credentials_change(
    app, client, make_user, db, monkeypatch, endpoint, change,
):
    user = make_user("sign_in_target", role="admin")
    finish = signin.finish

    def changed_after_verification(verified, **options):
        if change == "password_reset":
            accounts.set_password(user["id"], "reset password from administrator")
        else:
            accounts.delete(user)
        return finish(verified, **options)

    monkeypatch.setattr(signin, "finish", changed_after_verification)
    response = client.post(endpoint, data={"username": user["username"], "password": PASSWORD})
    assert response.status_code == 401
    assert db.scalar("SELECT COUNT(*) FROM user_sessions") == 0
    with client.session_transaction() as session:
        assert auth.SESSION_TOKEN_KEY not in session


@pytest.mark.parametrize("forced", [False, True])
def test_self_password_change_does_not_overwrite_a_concurrent_reset(
    app, client, make_user, login, db, monkeypatch, forced,
):
    user = make_user("password_target", force_password_change=int(forced))
    login(client, user)
    password_ok = profiles.password_ok
    replacement = "password set by administrator"

    def reset_after_verification(snapshot, supplied):
        accepted = password_ok(snapshot, supplied)
        accounts.set_password(snapshot["id"], replacement)
        return accepted

    monkeypatch.setattr(profiles, "password_ok", reset_after_verification)
    response = client.post("/force-change-password" if forced else "/settings/password", data={
        "current_password": PASSWORD, "new_password": "stale request password",
        "confirm_password": "stale request password", "username": "should_not_be_renamed" if forced else "",
    })
    assert response.status_code in (200, 302)
    assert passwords.verify_password(db.scalar("SELECT password FROM users WHERE id = ?", (user["id"],)),
                                     replacement)
    assert db.scalar("SELECT username FROM users WHERE id = ?", (user["id"],)) == user["username"]
    assert db.scalar("SELECT COUNT(*) FROM user_sessions WHERE revoked_at IS NULL") == 0


@pytest.mark.parametrize("change", ["password_reset", "superuser_granted"])
def test_self_deletion_rechecks_verified_credentials_and_account_protection(
    app, make_user, db, monkeypatch, change,
):
    user = make_user("delete_target")

    def changed_during_verification(snapshot, _password):
        if change == "password_reset":
            db.execute("UPDATE users SET password = 'new-password-hash' WHERE id = ?", (snapshot["id"],))
        else:
            db.execute("UPDATE users SET is_superuser = 1 WHERE id = ?", (snapshot["id"],))
        return True

    monkeypatch.setattr(profiles, "password_ok", changed_during_verification)
    with app.test_request_context(), connection_scope(), pytest.raises(profiles.ProfileError):
        profiles.delete_own_account(user, PASSWORD)
    assert db.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)) == 1


@pytest.mark.parametrize("change", ["target_admin", "target_owner", "target_superuser", "actor_demoted",
                                    "actor_suspended"])
def test_existing_impersonation_ends_when_account_authority_changes(
    app, admin_client, admin, make_user, db, change,
):
    target = make_user("impersonation_target")
    admin_client.post(f"/admin/users/{target['id']}/impersonate")
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs WHERE ended_at IS NULL") == 1
    account = target if change.startswith("target_") else admin
    field, value = {
        "target_admin": ("role", "admin"), "target_owner": ("role", "owner"),
        "target_superuser": ("is_superuser", 1), "actor_demoted": ("role", "user"),
        "actor_suspended": ("suspended", 1),
    }[change]
    db.update("users", {field: value}, "id = ?", (account["id"],))
    response = admin_client.get("/_probe/private")
    assert response.status_code in (200, 302)
    with admin_client.session_transaction() as session:
        assert "impersonator_id" not in session
        assert session["user_id"] == admin["id"]
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs WHERE ended_at IS NULL") == 0


def test_impersonation_ends_when_superuser_permission_is_removed(app, client, make_user, login, db):
    actor = make_user("superuser_actor", role="owner", is_superuser=1)
    target = make_user("admin_target", role="admin")
    login(client, actor)
    client.post(f"/admin/users/{target['id']}/impersonate")
    db.execute("UPDATE users SET is_superuser = 0 WHERE id = ?", (actor["id"],))
    assert client.get("/_probe/private").status_code == 200
    with client.session_transaction() as session:
        assert "impersonator_id" not in session
        assert session["user_id"] == actor["id"]
    assert db.scalar("SELECT COUNT(*) FROM impersonation_logs WHERE ended_at IS NULL") == 0


_MUTATIONS = {
    "rename": lambda actor, target: administration.rename(actor, target, "replacement_username"),
    "change_role": lambda actor, target: administration.change_role(actor, target, "editor"),
    "suspend": lambda actor, target: administration.suspend(
        actor, target, until=None, label="permanent", reason="", reason_visible=False, time_visible=False),
    "unsuspend": administration.unsuspend,
    "reset_password": lambda actor, target: administration.reset_password(
        actor, target, "replacement password", require_change=False, keep_original=True),
    "restore_password": administration.restore_password,
    "delete_user": administration.delete_user,
    "toggle_chat": administration.toggle_chat,
    "revoke_session": administration.revoke_session,
    "deattribute_all": administration.deattribute_all,
    "delete_role_history": administration.delete_role_history,
    "save_overrides": lambda actor, target: administration.save_overrides(
        actor, target, keys=[], read_restricted=False, read_ids=[], write_restricted=False, write_ids=[]),
    "reset_overrides": administration.reset_overrides,
}


@pytest.mark.parametrize("mutation", list(_MUTATIONS))
@pytest.mark.parametrize("change", ["target_promoted", "actor_demoted", "actor_password_reset"])
def test_admin_mutations_recheck_current_account_authority(app, make_user, db, mutation, change):
    actor = make_user("acting_admin", role="admin")
    target = make_user("mutable_target", original_password_backup="previous-password-hash")
    if change == "target_promoted":
        db.execute("UPDATE users SET role = 'owner' WHERE id = ?", (target["id"],))
    elif change == "actor_demoted":
        db.execute("UPDATE users SET role = 'user' WHERE id = ?", (actor["id"],))
    else:
        db.execute("UPDATE users SET password = 'new-password-hash' WHERE id = ?", (actor["id"],))
    before = db.one("SELECT * FROM users WHERE id = ?", (target["id"],))
    with app.test_request_context(), connection_scope(), pytest.raises(accounts.AccountError):
        _MUTATIONS[mutation](actor, target)
    assert db.one("SELECT * FROM users WHERE id = ?", (target["id"],)) == before


def test_superuser_mutation_rechecks_actor_permission(app, make_user, db):
    actor = make_user("acting_superuser", role="owner", is_superuser=1)
    target = make_user("normal_target")
    db.execute("UPDATE users SET is_superuser = 0 WHERE id = ?", (actor["id"],))
    with app.test_request_context(), connection_scope(), pytest.raises(accounts.AccountError):
        administration.toggle_superuser(actor, target)
    assert db.scalar("SELECT is_superuser FROM users WHERE id = ?", (target["id"],)) == 0


@pytest.mark.parametrize("change", ["owner_suspension", "demotion", "password_reset"])
def test_self_reactivation_rechecks_state_after_acquiring_write_lock(app, make_user, db, change):
    user = make_user("suspended_admin", role="admin", suspended=1)
    owner = make_user("owner_account", role="owner")
    with app.test_request_context(), connection_scope():
        transaction = wiki_db.session.transaction

        @contextmanager
        def changed_before_lock():
            if change == "owner_suspension":
                db.execute("INSERT INTO suspension_audit (user_id, action, performed_by, created_at) "
                           "VALUES (?, 'suspend', ?, '2026-10-01 00:00:00')", (user["id"], owner["id"]))
            else:
                field, value = ("role", "user") if change == "demotion" else ("password", "new-password-hash")
                db.update("users", {field: value}, "id = ?", (user["id"],))
            with transaction():
                yield

        wiki_db.session.transaction = changed_before_lock
        with pytest.raises(profiles.ProfileError):
            profiles.reactivate_self(user)
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (user["id"],)) == 1
