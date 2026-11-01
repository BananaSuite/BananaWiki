"""Regression tests for the UW findings of the full re-audit pass 15 (invite codes, assessment audit, admin reset)."""

from __future__ import annotations

from tests.hosting.conftest import make_account, make_wiki, portal, query, runtime  # noqa: F401


def _grant(db, user, *keys):
    from bananawiki.wiki import permissions as perms

    for key in sorted(perms.defaults(user["role"]) | set(keys)):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (user["id"], key))
    for access in ("read", "write"):
        db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, ?, 0)",
                   (user["id"], access))


def test_uw1_1_custom_code_tries_of_a_delegated_inviter_are_limited(app, client, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.admin import invites

    monkeypatch.setattr(invites, "CUSTOM_CODE_MAX_PER_USER", 3)
    boss = make_user("boss", role="admin")
    db.execute("INSERT INTO invite_codes (code, created_by, max_uses, assigned_role) VALUES ('STAFF2026', ?, 5, 'admin')",
               (boss["id"],))
    editor = make_user("editor1", role="editor")
    _grant(db, editor, "invite.generate", "invite.delete", "invite.view")
    login(client, editor)

    def probe(code):
        return client.post("/admin/codes/generate", data={"max_uses": "1", "expiry_mode": "24h", "custom_code": code})

    assert [probe(f"STAFF{year}").status_code for year in (2023, 2024, 2025)] == [302, 302, 302]
    answer = probe("STAFF2026")
    assert answer.status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM invite_codes WHERE created_by = ?", (editor["id"],)) == 3
    # A random code costs nothing, and an administrator is not limited.
    assert client.post("/admin/codes/generate", data={"max_uses": "1", "expiry_mode": "24h"}).status_code == 302
    client.post("/logout")
    login(client, boss)
    for year in range(2010, 2015):
        assert probe(f"STAFF{year}").status_code == 302


def test_uw1_2_deleting_an_assessment_or_resetting_attempts_is_audited(app, client, make_user, login, db):
    import json

    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.pages import service as pages
    from tests.features.test_assessments import _create_quiz, _questions, _submit_all

    with app.test_request_context(), connection_scope():
        page = pages.create("Fruit facts", "Bananas are berries.", author_id=None)
    editor = make_user("editor1", role="editor")
    readers = [make_user("reader1"), make_user("reader2")]
    _create_quiz(client, login, editor, page)
    _, rows = _questions(db, page)
    for reader in readers:
        login(client, reader)
        _submit_all(client, page, rows)
        client.post("/logout")
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    login(client, editor)
    assert client.post(f"/page/{page['slug']}/assessment/reset/{readers[0]['id']}").status_code == 302
    assert client.post(f"/page/{page['slug']}/assessment/delete").status_code == 302
    entries = db.all("SELECT actor_id, action, target_id, details FROM audit_log "
                     "WHERE id > ? AND action LIKE 'assessment.%' ORDER BY id", (mark,))
    assert [e["action"] for e in entries] == ["assessment.attempts_reset", "assessment.deleted"]
    assert all(e["actor_id"] == editor["id"] and e["target_id"] == str(page["id"]) for e in entries)
    reset, deleted = (json.loads(e["details"]) for e in entries)
    assert reset["member"] == "reader1" and reset["attempts"] == 1
    assert deleted["attempts"] == 1 and deleted["assessment"] == "Fruit quiz"


def test_uw2_1_a_reset_after_a_change_of_owner_deactivates_the_previous_owners_invite_codes(app, make_user, login, db):
    from bananawiki.ops import tenant_task
    from tests.conftest import PASSWORD

    db.execute("UPDATE site_settings SET bot_protection_enabled = 0")
    make_user("wikiadmin", role="owner")
    backdoor = make_user("backdoor", role="admin")
    editor = make_user("helper", role="editor")
    db.execute("INSERT INTO invite_codes (code, created_by, max_uses, assigned_role) VALUES "
               "('KEEPMEIN42', ?, 0, 'admin'), ('BACKDOORMEMBER', ?, 0, NULL), "
               "('HELPEREDITOR', ?, 0, 'editor'), ('HELPERMEMBER', ?, 0, NULL)",
               (backdoor["id"], backdoor["id"], editor["id"], editor["id"]))

    def live():
        return set(db.column("SELECT code FROM invite_codes WHERE deleted = 0"))

    tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 12345"})
    assert len(live()) == 4  # no change of owner: codes stay
    result = tenant_task.reset_admin(app.config["BW"], {"username": "wikiadmin", "password": "Temporary pass 67890",
                                                        "every_admin": True})
    assert result["invites_deactivated"] == 3 and live() == {"HELPERMEMBER"}
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'invite.deactivated'") == 3
    invite_ids = set(db.column("SELECT CAST(id AS TEXT) FROM invite_codes WHERE deleted = 1"))
    rows = db.all("SELECT target_type, target_id FROM audit_log WHERE action = 'invite.deactivated'")
    assert {row["target_type"] for row in rows} == {"invite"}  # as the wiki's own deactivation records it
    assert {row["target_id"] for row in rows} == invite_ids
    attacker = app.test_client()
    attacker.post("/signup", data={"username": "comeback", "password": PASSWORD, "confirm_password": PASSWORD,
                                   "invite_code": "KEEPMEIN42"})
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'comeback'") == 0


def test_uw2_2_a_duplicate_for_another_account_counts_as_a_change_of_owner(  # noqa: F811
        portal, runtime, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import accounts, instances
    from bananawiki.hosting.db import connection_scope

    source_owner = make_account("ownerx")
    new_owner = make_account("ownery")
    wiki = make_wiki(source_owner)

    def last_reset():
        return [call[0] for call in runtime.calls if call[0].startswith("reset_admins_")][-1]

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        own = instances.duplicate(instances.get(wiki["id"]), accounts.get(source_owner["id"]), "xcopy", "hosting",
                                  actor_id=None)
        instances.reset_admin_password(instances.get(own["id"]), actor_id=source_owner["id"])
        assert last_reset() == "reset_admins_named"  # the same owner's copy: as before
        copy = instances.duplicate(instances.get(wiki["id"]), accounts.get(new_owner["id"]), "ycopy", "hosting",
                                   actor_id=None)
        instances.reset_admin_password(instances.get(copy["id"]), actor_id=new_owner["id"])
        assert last_reset() == "reset_admins_every"


def test_uw2_2_a_same_owner_copy_of_a_wiki_that_changed_owner_inherits_the_change(  # noqa: F811
        portal, runtime, make_account, make_wiki, query):  # noqa: F811
    from bananawiki.hosting import accounts, instances
    from bananawiki.hosting.db import connection_scope

    first_owner = make_account("ownerw")
    new_owner = make_account("ownerz")
    wiki = make_wiki(first_owner)

    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.move_to_owner(instances.get(wiki["id"]), accounts.get(new_owner["id"]), actor_id=None)
        copy = instances.duplicate(instances.get(wiki["id"]), accounts.get(new_owner["id"]), "zcopy", "hosting",
                                   actor_id=None)
        instances.reset_admin_password(instances.get(copy["id"]), actor_id=new_owner["id"])
        resets = [call[0] for call in runtime.calls if call[0].startswith("reset_admins_")]
        assert resets[-1] == "reset_admins_every"  # the first owner's admin accounts are in the copy too
