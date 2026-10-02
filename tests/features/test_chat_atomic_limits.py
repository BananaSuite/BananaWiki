"""Chat limits must also hold when uploads arrive at the same time."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import storage
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.chat import retention, schema

from .chat_support import JSON, chat_folder, create_group, send, set_settings, start_dm, upload


def test_concurrent_dm_and_group_uploads_share_daily_limit(app, make_user, login, db, monkeypatch):
    sender = make_user("sender")
    make_user("recipient")
    clients = [app.test_client(), app.test_client()]
    for client in clients:
        login(client, sender)
    dm = start_dm(clients[0], "recipient")
    group = create_group(clients[0])
    set_settings(db, chat_attachments_per_day_limit=1)
    ready = Barrier(2)
    save = storage.save

    def simultaneous_save(*args, **kwargs):
        stored = save(*args, **kwargs)
        # Both requests have passed the early quota check and written their file.
        ready.wait(timeout=10)
        return stored

    monkeypatch.setattr(storage, "save", simultaneous_save)
    urls = [f"/chats/{dm}/send", f"/groups/{group}/send"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(send, client, url, "upload", attachment=upload())
                   for client, url in zip(clients, urls, strict=True)]
        replies = [request.result(timeout=15) for request in pending]
    assert sorted(reply.status_code for reply in replies) == [200, 429]
    assert db.scalar("SELECT (SELECT COUNT(*) FROM chat_attachments) + "
                     "(SELECT COUNT(*) FROM group_attachments)") == 1
    assert len(list(chat_folder(app).glob("*.txt"))) == 1


def test_concurrent_group_transfers_keep_one_owner(app, make_user, login, db, monkeypatch):
    from bananawiki.wiki.features.chat import routes_groups

    owner = make_user("owner")
    targets = [make_user("first"), make_user("second")]
    clients = [app.test_client(), app.test_client()]
    for client in clients:
        login(client, owner)
    group = create_group(clients[0])
    for target in targets:
        response = clients[0].post(f"/groups/{group}/members/add", data={"username": target["username"]},
                                    headers=JSON)
        assert response.status_code == 200
    ready = Barrier(2)
    target_of = routes_groups._target

    def simultaneous_target(*args, **kwargs):
        target = target_of(*args, **kwargs)
        ready.wait(timeout=10)
        return target

    monkeypatch.setattr(routes_groups, "_target", simultaneous_target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.post, f"/groups/{group}/transfer", data={"user_id": target["id"]},
                               headers=JSON) for client, target in zip(clients, targets, strict=True)]
        replies = [request.result(timeout=15) for request in pending]
    assert sorted(reply.status_code for reply in replies) == [200, 403]
    owners = db.column("SELECT user_id FROM group_members WHERE group_id = ? AND role = 'owner' AND banned = 0",
                       (group,))
    assert len(owners) == 1
    assert db.scalar("SELECT creator_id FROM group_chats WHERE id = ?", (group,)) == owners[0]


@pytest.mark.parametrize("kind", ["dm", "group"])
@pytest.mark.parametrize("action", ["delete_message", "clear"])
def test_deleting_uploaded_messages_does_not_reset_daily_quota(app, client, make_user, login, db, kind, action):
    sender = make_user("sender")
    make_user("recipient")
    login(client, sender)
    parent = start_dm(client, "recipient") if kind == "dm" else create_group(client)
    prefix = f"/chats/{parent}" if kind == "dm" else f"/groups/{parent}"
    table = "chat_attachments" if kind == "dm" else "group_attachments"
    set_settings(db, chat_attachments_per_day_limit=1)
    response = send(client, prefix + "/send", "upload", attachment=upload())
    assert response.status_code == 200
    removed = client.post(prefix + "/" + action, data={"message_id": response.json["message_id"]}, headers=JSON)
    assert removed.status_code == 200
    assert db.scalar(f"SELECT COUNT(*) FROM {table}") == 0
    assert not list(chat_folder(app).glob("*.txt"))
    assert send(client, prefix + "/send", "again", attachment=upload()).status_code == 429
    assert db.scalar("SELECT COUNT(*) FROM chat__upload_usage") == 1
    # A genuine expired allowance is available again and is pruned normally.
    db.execute("UPDATE chat__upload_usage SET created_at = ?", (sql_in(hours=-25),))
    with app.app_context(), connection_scope():
        retention.housekeeping()
    assert db.scalar("SELECT COUNT(*) FROM chat__upload_usage") == 0
    assert send(client, prefix + "/send", "next day", attachment=upload()).status_code == 200


def test_upload_usage_migrates_from_v4_and_is_removed_with_account(app, app_factory, client, make_user, login, db):
    sender = make_user("sender")
    make_user("recipient")
    login(client, sender)
    dm = start_dm(client, "recipient")
    group = create_group(client)
    assert send(client, f"/chats/{dm}/send", "dm", attachment=upload()).status_code == 200
    assert send(client, f"/groups/{group}/send", "group", attachment=upload()).status_code == 200
    dm_time, group_time = sql_in(hours=-2), sql_in(hours=-3)
    db.execute("UPDATE chat_attachments SET created_at = ?", (dm_time,))
    db.execute("UPDATE group_attachments SET created_at = ?", (group_time,))
    db.execute("DROP TABLE chat__upload_usage")
    db.execute("PRAGMA user_version = 4")
    upgraded = app_factory()
    assert db.scalar("PRAGMA user_version") == 5
    assert db.column("SELECT created_at FROM chat__upload_usage ORDER BY source") == [dm_time, group_time]
    # Re-running the hook neither doubles usage nor erases a deleted upload.
    schema.upgrade_v5(db.conn)
    db.execute("DELETE FROM chat_attachments")
    schema.upgrade_v5(db.conn)
    assert db.scalar("SELECT COUNT(*) FROM chat__upload_usage") == 2
    set_settings(db, chat_attachments_per_day_limit=2)
    upgraded_client = upgraded.test_client()
    login(upgraded_client, sender)
    assert send(upgraded_client, f"/chats/{dm}/send", "quota", attachment=upload()).status_code == 429
    db.execute("DELETE FROM users WHERE id = ?", (sender["id"],))
    assert db.scalar("SELECT COUNT(*) FROM chat__upload_usage") == 0


@pytest.mark.parametrize("restriction", ["ban", "timeout", "pause"])
def test_group_send_rechecks_membership_after_upload(app, make_user, login, db, monkeypatch, restriction):
    owner, sender = make_user("owner"), make_user("sender")
    owner_client, sender_client = app.test_client(), app.test_client()
    login(owner_client, owner)
    login(sender_client, sender)
    if restriction == "pause":
        group = db.scalar("SELECT id FROM group_chats WHERE is_global = 1")
        sender_client.post("/groups/global")
    else:
        group = create_group(owner_client)
        owner_client.post(f"/groups/{group}/members/add", data={"username": sender["username"]}, headers=JSON)
    save = storage.save

    def restricted_during_upload(*args, **kwargs):
        stored = save(*args, **kwargs)
        if restriction == "ban":
            db.execute("UPDATE group_members SET banned = 1 WHERE group_id = ? AND user_id = ?",
                       (group, sender["id"]))
        elif restriction == "timeout":
            db.execute("UPDATE group_members SET timed_out_until = ? WHERE group_id = ? AND user_id = ?",
                       (sql_in(hours=1), group, sender["id"]))
        else:
            db.execute("UPDATE group_chats SET is_active = 0 WHERE id = ?", (group,))
        return stored

    monkeypatch.setattr(storage, "save", restricted_during_upload)
    reply = send(sender_client, f"/groups/{group}/send", "must not arrive", attachment=upload())
    assert reply.status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM group_messages WHERE sender_id = ?", (sender["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM group_attachments") == 0
    assert db.scalar("SELECT COUNT(*) FROM chat__upload_usage") == 0
    assert not list(chat_folder(app).glob("*.txt"))
