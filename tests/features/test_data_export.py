"""Personal data export (user_data_export feature)."""

from __future__ import annotations

import io
import json
import zipfile

from .people_support import image_bytes, make_page, set_feature


def _archive(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.data[:300]
    assert response.mimetype == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.get_data()))


def test_own_export_contains_account_data_without_secrets(app, client, make_user, login, db):
    bob = make_user("bob", role="editor")
    make_page(app, "Doc", "my words", author_id=bob["id"])
    login(client, bob)
    client.post("/settings/profile", data={"real_name": "Bob", "avatar": (io.BytesIO(image_bytes()), "a.png")},
                content_type="multipart/form-data")
    response = client.get("/settings/export")
    assert response.is_streamed
    archive = _archive(response)
    names = archive.namelist()
    account = json.loads(archive.read("account.json"))
    assert account["username"] == "bob" and "password" not in account
    history = json.loads(archive.read("data/page_history.json"))
    assert [row["content"] for row in history] == ["my words"]
    sessions = json.loads(archive.read("data/user_sessions.json"))
    assert sessions and "token_hash" not in sessions[0]
    assert json.loads(archive.read("profile/profile.json"))["real_name"] == "Bob"
    assert any(name.startswith("files/avatars/") for name in names)
    assert b"correct horse" not in b"".join(archive.read(n) for n in names)
    assert json.loads(archive.read("manifest.json"))["format"] == "bananawiki-user-data-export"


def test_export_leaves_out_other_accounts(app, client, make_user, login):
    bob, eve = make_user("bob", role="editor"), make_user("eve", role="editor")
    make_page(app, "Eve's page", "secret of eve", author_id=eve["id"])
    login(client, bob)
    archive = _archive(client.get("/settings/export"))
    assert b"secret of eve" not in b"".join(archive.read(n) for n in archive.namelist())


def test_legacy_export_url(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/account/export").headers["Location"].endswith("/settings/export")


def test_admin_exports_a_member(admin_client, make_user):
    bob = make_user("bob")
    archive = _archive(admin_client.get(f"/admin/users/{bob['id']}/export"))
    assert json.loads(archive.read("account.json"))["id"] == bob["id"]
    assert admin_client.get("/admin/users/nobody/export").status_code == 404


def test_admins_cannot_export_owners_or_protected_accounts(admin_client, make_user):
    owner = make_user("the_owner", role="owner")
    keeper = make_user("keeper", is_superuser=1)
    assert admin_client.get(f"/admin/users/{owner['id']}/export").status_code == 403
    assert admin_client.get(f"/admin/users/{keeper['id']}/export").status_code == 403


def test_members_cannot_export_others(client, make_user, login):
    bob = make_user("bob")
    login(client, make_user("eve"))
    assert client.get(f"/admin/users/{bob['id']}/export").status_code == 403


def test_exports_are_rate_limited(client, make_user, login):
    login(client, make_user("bob"))
    for _ in range(5):
        assert client.get("/settings/export").status_code == 200
    assert client.get("/settings/export").status_code == 429


def test_feature_switch(app, client, make_user, login):
    set_feature(app, "user_data_export", False)
    login(client, make_user("bob"))
    assert client.get("/settings/export").status_code == 404
    assert b"Download my data" not in client.get("/settings").data
