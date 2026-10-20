"""Wiki lifecycle from the dashboard, tenant isolation, collaborators and ownership transfers."""

from __future__ import annotations

import re

import pytest

USE_CASE = "A small team handbook for our volunteers."


def _create(client, slug: str, **extra):
    data = {"subdomain": slug, "declared_use_case": USE_CASE, "compliance_declared": "1"}
    data.update(extra)
    return client.post("/instances/create", data=data)


def test_create_shows_credentials_once_and_never_stores_them(web, make_account, login, runtime, query):
    owner = make_account()
    login(web, owner)
    response = _create(web, "handbook")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    html = response.get_data(as_text=True)
    row = query("SELECT id, subdomain, status, port, admin_username, admin_password_plain FROM instances", one=True)
    assert row["subdomain"] == "handbook" and row["status"] == "running"
    assert row["admin_password_plain"] is None
    password = runtime.tenants["handbook"].passwords[row["admin_username"]]
    assert password in html
    assert runtime.called("provision") == ["handbook"]
    assert password not in web.get(f"/instances/{row['id']}").get_data(as_text=True)


@pytest.mark.parametrize("filled", [{"admin_username": "chief"}, {"admin_password": "a long secret phrase 42"},
                                    {"admin_confirm_password": "a long secret phrase 42"}])
def test_an_administrator_login_typed_without_the_tick_box_is_refused(web, make_account, login, runtime, query,
                                                                      filled):
    # Without JavaScript the fields stay usable while the box is unticked: refuse instead of silently
    # generating a login the owner did not expect.
    owner = make_account()
    login(web, owner)
    response = _create(web, "unticked", **filled)
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert "Choose the administrator login myself" in page and "clear the fields" in page
    assert "<details open><summary>Administrator login (optional)</summary>" in page
    assert query("SELECT COUNT(*) AS n FROM instances", one=True)["n"] == 0
    assert runtime.called("provision") == []
    # The box ticked, the chosen login is used; blank fields without the box still get a generated one.
    response = _create(web, "ticked", set_admin_credentials="1", admin_username="chief",
                       admin_password="a long secret phrase 42", admin_confirm_password="a long secret phrase 42")
    assert response.status_code == 200 and runtime.tenants["ticked"].passwords == {"chief": "a long secret phrase 42"}
    response = _create(web, "generated", admin_username=" ", admin_password="", admin_confirm_password="")
    assert response.status_code == 200 and "chief" not in runtime.tenants["generated"].passwords


@pytest.mark.parametrize("extra", [
    {"subdomain": "ab"}, {"subdomain": "Bad_Name"}, {"subdomain": "double--dash"}, {"subdomain": "admin"},
    {"declared_use_case": "short"}, {"compliance_declared": ""}, {"domain_mode": "apex"},
])
def test_create_validates_name_use_case_and_compliance(web, make_account, login, query, extra):
    owner = make_account()
    login(web, owner)
    assert _create(web, extra.pop("subdomain", "valid-name"), **extra).status_code == 400
    assert query("SELECT COUNT(*) AS n FROM instances", one=True)["n"] == 0


def test_an_apex_name_cannot_take_a_hosting_wikis_address(ctx, make_account, make_wiki):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    make_wiki(make_account(), "team")
    admin = make_account(admin=True)
    with pytest.raises(ServiceError):
        instances.create(admin, "team-hosting", domain_mode="apex")


def test_a_name_is_shortened_to_fit_a_dns_label_with_the_suffix(ctx, query):
    from bananawiki.hosting import urls
    from bananawiki.hosting.errors import ServiceError

    query("UPDATE hosting_settings SET instance_url_suffix = ? WHERE id = 1", ("s" * 30,))
    room = 63 - 30 - 1
    assert urls.validate_slug("a" * room, account_is_admin=False) == "a" * room
    with pytest.raises(ServiceError) as error:
        urls.validate_slug("a" * (room + 1), account_is_admin=False)
    assert error.value.values["max"] == room
    assert urls.validate_slug("a" * (room + 1), account_is_admin=True, domain_mode="apex")


def test_create_rolls_back_when_the_runtime_fails(web, make_account, login, runtime, query):
    owner = make_account()
    login(web, owner)
    runtime.fail_next("provision", "no_space")
    response = _create(web, "failing")
    assert response.status_code == 400
    assert "disk space" in response.get_data(as_text=True)
    row = query("SELECT status, terminated_reason FROM instances", one=True)
    assert row == {"status": "terminated", "terminated_reason": "provisioning_failed"}
    assert _create(web, "failing").status_code == 200, "the name is free again"


def test_account_limit_is_enforced(tmp_path, make_account):
    from .hosting_support import build_portal, portal_environ

    app = build_portal(tmp_path / "limit", environ=portal_environ(tmp_path / "limit", MAX_INSTANCES_PER_ACCOUNT="1"))
    client = app.test_client()
    with app.app_context():
        from bananawiki.hosting import accounts
        from bananawiki.hosting.db import connection_scope

        with connection_scope():
            accounts.create("limited", "correct horse 42")
    client.post("/login", data={"username": "limited", "password": "correct horse 42"})
    assert _create(client, "first-wiki").status_code == 200
    assert _create(client, "second-wiki").status_code == 400


def test_other_accounts_cannot_see_or_touch_a_wiki(portal, make_account, make_wiki, login, runtime):
    owner, stranger = make_account(), make_account()
    wiki = make_wiki(owner, "private-wiki")
    client = portal.test_client()
    login(client, stranger)
    for path in ("", "/collaborators", "/analytics", "/domain"):
        assert client.get(f"/instances/{wiki['id']}{path}").status_code == 404, path
    for action in ("stop", "restart", "terminate", "reset-password", "reset-wiki", "download", "transfer"):
        assert client.post(f"/instances/{wiki['id']}/{action}").status_code == 404, action
    assert client.post(f"/instances/{wiki['id']}/collaborators/add", data={"username": stranger["username"]}).status_code == 404
    client.post("/instances/bulk-stop", data={"instance_ids": [wiki["id"]]})
    assert runtime.called("stop") == []
    assert "private-wiki" not in client.get("/dashboard").get_data(as_text=True)


def test_stop_restart_and_terminate(web, make_account, make_wiki, login, runtime, query):
    owner = make_account()
    wiki = make_wiki(owner, "cycle")
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/stop")
    assert query("SELECT status FROM instances", one=True)["status"] == "stopped"
    assert runtime.tenants["cycle"].running is False
    web.post(f"/instances/{wiki['id']}/restart")
    assert query("SELECT status FROM instances", one=True)["status"] == "running"
    web.post(f"/instances/{wiki['id']}/terminate")
    row = query("SELECT subdomain, status, terminated_at FROM instances", one=True)
    assert row["status"] == "terminated" and row["terminated_at"]
    assert re.fullmatch(r"cycle--terminated-[0-9a-z]{8}", row["subdomain"])
    assert web.get(f"/instances/{wiki['id']}").status_code == 404


def test_reset_admin_password_shows_a_new_password(web, make_account, make_wiki, login, runtime):
    owner = make_account()
    wiki = make_wiki(owner, "resetme")
    login(web, owner)
    response = web.post(f"/instances/{wiki['id']}/reset-password")
    assert response.status_code == 200
    username = wiki["admin_username"]
    assert runtime.tenants["resetme"].passwords[username] in response.get_data(as_text=True)


def test_suspended_wiki_is_locked_for_its_owner(web, make_account, make_wiki, login, query, runtime):
    owner = make_account()
    wiki = make_wiki(owner, "locked")
    query("UPDATE instances SET status = 'suspended' WHERE id = ?", (wiki["id"],))
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/restart")
    web.post(f"/instances/{wiki['id']}/terminate")
    assert query("SELECT status FROM instances", one=True)["status"] == "suspended"
    assert runtime.called("start") == []



def test_a_suspension_during_the_owners_start_leaves_the_wiki_stopped(web, make_account, make_wiki, login, query,
                                                                       runtime, monkeypatch):
    owner = make_account()
    wiki = make_wiki(owner, "racing")
    query("UPDATE instances SET status = 'stopped' WHERE id = ?", (wiki["id"],))
    runtime.tenants["racing"].running = False
    started = runtime.start

    def suspended_meanwhile(spec):
        # An administrator suspends (row updated, container stopped) before the owner's start finishes.
        query("UPDATE instances SET status = 'suspended', suspended_at = '2026-01-01 00:00:00' WHERE id = ?",
              (wiki["id"],))
        runtime.stop(spec)
        started(spec)

    monkeypatch.setattr(runtime, "start", suspended_meanwhile)
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/restart")
    assert query("SELECT status FROM instances", one=True)["status"] == "suspended"
    assert runtime.tenants["racing"].running is False

# Collaborators


def test_collaborator_gets_only_the_granted_permissions(portal, make_account, make_wiki, login, runtime, query):
    owner, helper = make_account(), make_account()
    wiki = make_wiki(owner, "shared")
    owner_client, helper_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(helper_client, helper)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": helper["username"], "role": "custom", "permissions": ["start_stop"]})
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 200
    assert "shared" in helper_client.get("/dashboard").get_data(as_text=True)
    helper_client.post(f"/instances/{wiki['id']}/stop")
    assert runtime.called("stop") == ["shared"]
    assert helper_client.post(f"/instances/{wiki['id']}/terminate").status_code == 404
    assert helper_client.get(f"/instances/{wiki['id']}/collaborators").status_code == 404
    assert query("SELECT status FROM instances", one=True)["status"] == "stopped"


def test_collaborator_manager_cannot_grant_more_than_they_hold(portal, make_account, make_wiki, login, query):
    owner, manager, newcomer = make_account(), make_account(), make_account()
    wiki = make_wiki(owner, "delegated")
    owner_client, manager_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(manager_client, manager)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add", data={
        "username": manager["username"], "role": "custom", "permissions": ["manage_collaborators", "start_stop"]})
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "full_access"})
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "custom", "permissions": ["terminate"]})
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators WHERE account_id = ?",
                 (newcomer["id"],), one=True)["n"] == 0
    manager_client.post(f"/instances/{wiki['id']}/collaborators/add",
                        data={"username": newcomer["username"], "role": "custom", "permissions": ["start_stop"]})
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators WHERE account_id = ?",
                 (newcomer["id"],), one=True)["n"] == 1


def test_owner_can_remove_a_collaborator(portal, make_account, make_wiki, login):
    owner, helper = make_account(), make_account()
    wiki = make_wiki(owner, "removal")
    owner_client, helper_client = portal.test_client(), portal.test_client()
    login(owner_client, owner)
    login(helper_client, helper)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add",
                      data={"username": helper["username"], "role": "full_access"})
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 200
    owner_client.post(f"/instances/{wiki['id']}/collaborators/{helper['id']}/remove")
    assert helper_client.get(f"/instances/{wiki['id']}").status_code == 404


# Ownership transfer


def test_transfer_needs_the_recipients_acceptance(portal, make_account, make_wiki, login, query):
    owner, recipient, stranger = make_account(), make_account(), make_account()
    wiki = make_wiki(owner, "gift")
    owner_client, recipient_client, stranger_client = (portal.test_client() for _ in range(3))
    login(owner_client, owner)
    login(recipient_client, recipient)
    login(stranger_client, stranger)
    owner_client.post(f"/instances/{wiki['id']}/transfer", data={"username": recipient["username"]})
    transfer = query("SELECT id FROM instance_ownership_transfers", one=True)
    assert transfer is not None
    stranger_client.post(f"/transfers/{transfer['id']}/accept")
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == owner["id"]
    recipient_client.post(f"/transfers/{transfer['id']}/accept")
    assert query("SELECT account_id FROM instances", one=True)["account_id"] == recipient["id"]
    # The previous owner keeps no access unless the recipient chooses so (see test_hosting_access.py).
    assert query("SELECT role FROM instance_collaborators WHERE account_id = ?", (owner["id"],), one=True) is None
    assert stranger_client.get(f"/instances/{wiki['id']}").status_code == 404


def test_transfer_to_a_suspended_account_is_refused(web, make_account, make_wiki, login, query):
    owner, blocked = make_account(), make_account(suspended=1)
    wiki = make_wiki(owner, "nogift")
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/transfer", data={"username": blocked["username"]})
    assert query("SELECT COUNT(*) AS n FROM instance_ownership_transfers", one=True)["n"] == 0


# Data directories


def _orphan(make_account, make_wiki, query, slug: str) -> None:
    """Data left under *slug* with no wiki holding the name (what an interrupted move can leave)."""
    wiki = make_wiki(make_account(), slug)
    query("UPDATE instances SET status = 'terminated', subdomain = ? WHERE id = ?",
          (f"{slug}--terminated-{wiki['id'][:8]}", wiki["id"]))


@pytest.mark.parametrize("operation", ["create", "duplicate", "duplicate_without_source", "import"])
def test_a_failed_creation_never_deletes_data_it_did_not_create(ctx, make_account, make_wiki, runtime, query,
                                                                 tmp_path, operation):
    import zipfile

    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    owner = make_account()
    with pytest.raises(ServiceError) as error:
        if operation == "create":
            instances.create(owner, "alpha")
        elif operation.startswith("duplicate"):
            source = make_wiki(owner, "source-wiki")
            if operation == "duplicate_without_source":
                # The taken target must be reported first, or the cleanup would destroy it.
                del runtime.tenants["source-wiki"]
            instances.duplicate(source, owner, "alpha", "hosting", actor_id=owner["id"])
        else:
            archive = tmp_path / "site.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("bananawiki.json", "{}")
            instances.import_archive(owner, "alpha", "hosting", archive, actor_id=owner["id"])
    assert error.value.key == "hosting.runtime.data_exists"
    assert runtime.tenants["alpha"] is orphaned, "someone else's data stays where it was"
    assert "alpha" not in runtime.called("destroy")
    row = query("SELECT status, terminated_reason FROM instances WHERE account_id = ? AND status = 'terminated'",
                (owner["id"],), one=True)
    assert row == {"status": "terminated", "terminated_reason": "provisioning_failed"}


def _assert_left_alone(runtime, orphaned, instance_id: str) -> None:
    from bananawiki.hosting import instances

    assert runtime.tenants["alpha"] is orphaned, "someone else's data stays where it was"
    assert "alpha" not in runtime.called("destroy")
    row = instances.get(instance_id)
    assert (row["status"], row["terminated_reason"], row["port"]) == ("terminated", "provisioning_failed", None)
    assert row["provisioning_state"] == "failed" and row["data_retained_until"] is None
    assert instances.by_slug("alpha", "hosting") is None


def test_cancelling_a_creation_that_never_reached_the_runtime_leaves_the_name_alone(ctx, make_account, make_wiki,
                                                                                   runtime, query, monkeypatch):
    """The creator gave up on a busy provisioning lock: nothing under the name is the wiki's to delete."""
    from filelock import FileLock, Timeout

    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    owner = make_account()
    provisioned = runtime.called("provision")

    def busy(self, *_args, **_kwargs):
        raise Timeout(self.lock_file)

    with monkeypatch.context() as patch:
        patch.setattr(FileLock, "acquire", busy)
        with pytest.raises(ServiceError, match="provisioning_busy"):
            instances.create(owner, "alpha")
    pending = instances.by_slug("alpha", "hosting")
    assert pending["provisioning_state"] == "pending" and runtime.called("provision") == provisioned
    instances.terminate(pending, actor_id=owner["id"])
    _assert_left_alone(runtime, orphaned, pending["id"])


def test_an_abandoned_creation_that_expires_leaves_the_name_alone(ctx, make_account, make_wiki, runtime, query):
    """The creator was killed right after its reservation; nobody cancels it, the expiry sweep does."""
    from bananawiki.hosting import instances

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    pending = instances._insert(make_account(), "alpha", "hosting", admin_username="admin", custom_credentials=False,
                                easy_wiki=False, use_case="")
    query("UPDATE instances SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (pending["id"],))
    assert instances.terminate_expired() == 1
    _assert_left_alone(runtime, orphaned, pending["id"])


@pytest.mark.parametrize("failures", [1, 2])
def test_a_refused_creation_whose_row_update_fails_never_deletes_the_folder_later(ctx, make_account, make_wiki,
                                                                                runtime, query, monkeypatch,
                                                                                failures):
    """``data_exists``, then the update terminating the row fails: the cancellation that follows still knows."""
    import sqlite3

    from bananawiki.core.sqlite import Session
    from bananawiki.hosting import instances

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    owner = make_account()
    update, left = Session.update, {"failures": failures}

    def locked(self, table, values, where, params=()):
        if table == "instances" and values.get("terminated_reason") == "provisioning_failed" and left["failures"]:
            left["failures"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return update(self, table, values, where, params)

    monkeypatch.setattr(Session, "update", locked)
    with pytest.raises(sqlite3.OperationalError):
        instances.create(owner, "alpha")
    row = instances.by_slug("alpha", "hosting")
    assert row["status"] == "stopped", "the row still holds the name"
    for _ in range(failures - 1):
        with pytest.raises(sqlite3.OperationalError):
            instances.terminate(instances.get(row["id"]), actor_id=None)
    instances.terminate(instances.get(row["id"]), actor_id=None)
    _assert_left_alone(runtime, orphaned, row["id"])


@pytest.mark.parametrize("cancel", ["terminate", "expiry"])
def test_a_refused_creation_whose_refusal_event_fails_never_deletes_the_folder_later(ctx, make_account, make_wiki,
                                                                                    runtime, query, monkeypatch,
                                                                                    cancel):
    """``data_exists``, then the first database write about it fails: the marker file written before still tells."""
    import sqlite3

    from bananawiki.hosting import events, instances

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    owner = make_account()
    record, left = events.record, {"failures": 1}

    def locked(subject_type, subject_id, action, actor_id=None, reason=""):
        if action == instances.PROVISIONING_REFUSED and left["failures"]:
            left["failures"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return record(subject_type, subject_id, action, actor_id, reason)

    monkeypatch.setattr(events, "record", locked)
    with pytest.raises(sqlite3.OperationalError):
        instances.create(owner, "alpha")
    row = instances.by_slug("alpha", "hosting")
    assert (row["status"], row["provisioning_state"]) == ("stopped", "pending"), "the row still holds the name"
    assert not query("SELECT 1 FROM hosting_events WHERE subject_id = ? AND action = ?",
                     (row["id"], instances.PROVISIONING_REFUSED))
    marker = instances._refusal_marker(row["id"])
    assert marker.is_file() and marker.read_text(encoding="utf-8").startswith("data_exists ")
    assert (marker.stat().st_mode & 0o777, marker.parent.stat().st_mode & 0o777) == (0o600, 0o700)
    if cancel == "expiry":
        query("UPDATE instances SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (row["id"],))
        assert instances.terminate_expired() == 1
    else:
        instances.terminate(instances.get(row["id"]), actor_id=owner["id"])
    _assert_left_alone(runtime, orphaned, row["id"])


def test_a_refusal_whose_marker_cannot_be_written_is_still_recorded_in_the_database(ctx, make_account, make_wiki,
                                                                                    runtime, query, monkeypatch):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError
    from bananawiki.ops import files

    _orphan(make_account, make_wiki, query, "alpha")
    orphaned = runtime.tenants["alpha"]
    owner = make_account()

    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files, "atomic_write", full)
    with pytest.raises(ServiceError) as error:
        instances.create(owner, "alpha")
    assert error.value.key == "hosting.runtime.data_exists"
    row = query("SELECT id FROM instances WHERE account_id = ?", (owner["id"],), one=True)
    assert not instances._refusal_marker(row["id"]).exists()
    _assert_left_alone(runtime, orphaned, row["id"])


def test_a_refusal_marker_that_cannot_be_checked_keeps_a_started_creation_reserved(ctx, make_account, runtime,
                                                                                  monkeypatch):
    """The runtime worked on the creation and its container may still run: an unreadable marker directory must
    not make the failure look refused, which would release the port and name without stopping anything."""
    from pathlib import Path

    from bananawiki.hosting import instances
    from bananawiki.hosting.runtime import RuntimeFailure

    folder = Path(instances.cfg().platform_state_dir) / ".provisioning-refused"
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("not a directory")  # every check inside it fails with ENOTDIR
    owner = make_account()
    provision = runtime.provision

    def started_then_failed(*args, **kwargs):
        provision(*args, **kwargs)
        raise RuntimeFailure("start_failed", "synthetic")

    monkeypatch.setattr(runtime, "provision", started_then_failed)
    runtime.fail_next("destroy", "unavailable")
    runtime.fail_next("stop", "unavailable")
    with pytest.raises(NotADirectoryError):
        instances.create(owner, "beta")
    row = instances.by_slug("beta", "hosting")
    assert row["status"] != "terminated" and row["port"] is not None and runtime.tenants["beta"].running


def test_cancelling_a_creation_the_runtime_worked_on_still_deletes_its_data(ctx, make_account, runtime,
                                                                           monkeypatch):
    """The other side of the rule: data the runtime created for an interrupted creation is cleaned up."""
    from bananawiki.hosting import instances

    provision = runtime.provision

    def interrupted(*args, **kwargs):
        provision(*args, **kwargs)
        raise KeyboardInterrupt("worker killed after the seed")

    monkeypatch.setattr(runtime, "provision", interrupted)
    with pytest.raises(KeyboardInterrupt):
        instances.create(make_account(), "alpha")
    pending = instances.by_slug("alpha", "hosting")
    assert pending["provisioning_state"] == "pending" and "alpha" in runtime.tenants
    instances.terminate(pending, actor_id=None)
    assert "alpha" in runtime.called("destroy") and "alpha" not in runtime.tenants
    assert instances.get(pending["id"])["status"] == "terminated"


def test_terminate_keeps_the_name_until_the_data_has_moved(ctx, make_account, make_wiki, runtime):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    owner, newcomer = make_account(), make_account()
    wiki = make_wiki(owner, "alpha")
    runtime.fail_next("relocate")
    with pytest.raises(ServiceError):
        instances.terminate(instances.get(wiki["id"]), actor_id=None)
    row = instances.get(wiki["id"])
    assert (row["subdomain"], row["status"]) == ("alpha", "stopped"), "the wiki still holds its name"
    with pytest.raises(ServiceError) as error:
        instances.create(newcomer, "alpha")
    assert error.value.key == "hosting.instances.name_taken"
    instances.terminate(row, actor_id=None)
    assert instances.get(wiki["id"])["status"] == "terminated"
    restored = instances.restore(instances.get(wiki["id"]), actor_id=owner["id"])
    assert restored["subdomain"] == "alpha" and runtime.tenants["alpha"].running


def test_terminate_does_not_release_a_name_its_data_moved_away_from(ctx, make_account, make_wiki, runtime):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    owner = make_account()
    wiki = make_wiki(owner, "before")
    stale = instances.get(wiki["id"])
    instances.rename(stale, "after", "hosting", actor_id=owner["id"])
    with pytest.raises(ServiceError) as error:
        instances.terminate(stale, actor_id=None)
    assert error.value.key == "hosting.instances.changed_state"
    row = instances.get(wiki["id"])
    assert (row["subdomain"], row["status"]) == ("after", "running"), "the row still holds its data's name"
    assert runtime.tenants["after"].running


def test_terminate_without_data_or_grace_period(ctx, make_account, make_wiki, runtime):
    from bananawiki.hosting import instances, settings

    owner = make_account()
    empty = make_wiki(owner, "empty")
    del runtime.tenants["empty"]
    instances.terminate(instances.get(empty["id"]), actor_id=None)
    assert instances.get(empty["id"])["status"] == "terminated", "missing data does not block a termination"
    settings.update(grace_period_days=0)
    brief = make_wiki(owner, "brief")
    instances.terminate(instances.get(brief["id"]), actor_id=None)
    archived = instances.get(brief["id"])["subdomain"]
    assert runtime.called("relocate")[-1] == f"brief->{archived}"
    assert runtime.called("destroy")[-1] == archived and archived not in runtime.tenants


def test_a_restored_wiki_gets_the_usual_expiry_of_its_owner(ctx, make_account, make_wiki):
    from datetime import timedelta

    from bananawiki.core.timeutil import parse, utcnow
    from bananawiki.hosting import instances

    admin, customer = make_account(admin=True), make_account()
    for owner, slug in ((admin, "admins-own"), (customer, "customers-own")):
        wiki = make_wiki(owner, slug)
        instances.terminate(instances.get(wiki["id"]), actor_id=None)
        restored = instances.restore(instances.get(wiki["id"]), actor_id=admin["id"])
        left = parse(restored["expires_at"]) - utcnow()
        if owner is admin:
            assert left > timedelta(days=365 * 50), "an administrator's wiki does not expire"
        else:
            assert left <= timedelta(days=instances.cfg().limits.duration_days)
        instances.terminate(instances.get(wiki["id"]), actor_id=None)
        restored = instances.restore(instances.get(wiki["id"]), actor_id=admin["id"], extend_days=3)
        assert parse(restored["expires_at"]) - utcnow() <= timedelta(days=3), "the days given win"


def test_restore_and_rename_put_the_data_back_when_the_row_cannot_follow(ctx, make_account, make_wiki, runtime,
                                                                         monkeypatch):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    owner = make_account()
    gone = make_wiki(owner, "comeback")
    instances.terminate(instances.get(gone["id"]), actor_id=None)
    archived = instances.get(gone["id"])["subdomain"]

    def no_ports():
        raise ServiceError("hosting.instances.no_ports")

    with monkeypatch.context() as patch:
        patch.setattr(instances, "_allocate_port", no_ports)
        with pytest.raises(ServiceError):
            instances.restore(instances.get(gone["id"]), actor_id=owner["id"])
    assert archived in runtime.tenants and "comeback" not in runtime.tenants
    assert instances.get(gone["id"])["status"] == "terminated"

    moving = make_wiki(owner, "original")
    make_wiki(owner, "racer")
    del runtime.tenants["racer"]
    # The name check passed just before another wiki took the name.
    monkeypatch.setattr(instances, "name_holder", lambda slug, mode: None)
    with pytest.raises(ServiceError) as error:
        instances.rename(instances.get(moving["id"]), "racer", "hosting", actor_id=owner["id"])
    assert error.value.key == "hosting.instances.name_taken"
    assert instances.get(moving["id"])["subdomain"] == "original"
    assert "racer" not in runtime.tenants and runtime.tenants["original"].running


def test_rename_puts_the_data_back_when_the_database_is_busy(ctx, make_account, make_wiki, runtime, monkeypatch):
    import sqlite3

    from bananawiki.hosting import instances

    owner = make_account()
    moving = make_wiki(owner, "original")
    real = instances.db

    class Busy:
        def __getattr__(self, name):
            return getattr(real, name)

        def update(self, table, values, *args, **kwargs):
            if table == "instances" and values.get("subdomain") == "renamed":
                raise sqlite3.OperationalError("database is locked")
            return real.update(table, values, *args, **kwargs)

    monkeypatch.setattr(instances, "db", Busy())
    with pytest.raises(sqlite3.OperationalError):
        instances.rename(instances.get(moving["id"]), "renamed", "hosting", actor_id=owner["id"])
    assert instances.get(moving["id"])["subdomain"] == "original"
    assert "renamed" not in runtime.tenants and runtime.tenants["original"].running


def test_a_rename_whose_folder_move_fails_starts_the_wiki_again(ctx, make_account, make_wiki, runtime):
    """When the folder cannot be moved, the rename is undone and the wiki starts again under its old name."""
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError
    from bananawiki.hosting.runtime.fake import FakeTenant

    owner = make_account()
    wiki = make_wiki(owner, "handbook")
    runtime.tenants["docs"] = FakeTenant(runtime.tenants["handbook"].spec)
    with pytest.raises(ServiceError) as error:
        instances.rename(instances.get(wiki["id"]), "docs", "hosting", actor_id=owner["id"])
    assert error.value.key == "hosting.runtime.data_exists"
    row = instances.get(wiki["id"])
    assert (row["subdomain"], row["status"]) == ("handbook", "running")
    assert runtime.tenants["handbook"].running and not runtime.tenants["docs"].running

    runtime.fail_next("start")
    with pytest.raises(ServiceError):
        instances.rename(row, "docs", "hosting", actor_id=owner["id"])
    assert instances.get(wiki["id"])["status"] == "stopped", "a wiki that did not start is not listed as running"
    assert not runtime.tenants["handbook"].running


def test_moving_an_apex_wiki_to_hosting_mode_starts_it_again_when_its_folder_cannot_move(ctx, make_account,
                                                                                      make_wiki, runtime):
    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError
    from bananawiki.hosting.runtime.fake import FakeTenant

    admin = make_account(admin=True)
    wiki = make_wiki(admin, "lead", domain_mode="apex")
    runtime.tenants["lead"] = FakeTenant(runtime.tenants["lead__apex"].spec)
    with pytest.raises(ServiceError) as error:
        instances.rename_for_non_admin(instances.get(wiki["id"]), actor_id=None)
    assert error.value.key == "hosting.runtime.data_exists"
    row = instances.get(wiki["id"])
    assert (row["subdomain"], row["domain_mode"], row["status"]) == ("lead", "apex", "running")
    assert runtime.tenants["lead__apex"].running
