"""Page attachments: upload, download, ZIP, delete, permissions and clean-up."""

import io
import os
import zipfile

from bananawiki.wiki.features.attachments import service

from .pages_support import in_app, make_category, make_page, restrict, set_feature


def _upload(client, page, name="notes.txt", data=b"hello", **extra):
    return client.post(f"/page/{page['slug']}/attachments",
                       data={"files": (io.BytesIO(data), name), **extra}, content_type="multipart/form-data")


def _api_upload(client, page, name="notes.txt", data=b"hello"):
    return client.post(f"/api/page/{page['id']}/attachments", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", headers={"Accept": "application/json"})


def _rows(db, page):
    return db.all("SELECT * FROM page_attachments WHERE page_id = ? ORDER BY id", (page["id"],))


def _folder(app):
    return app.config["BW"].folders.attachments


def test_editor_uploads_lists_and_downloads(app, client, make_user, login, db):
    page = make_page(app, "Guide", "text")
    editor = make_user("att_ed", role="editor")
    login(client, editor)
    response = _upload(client, page, "report.pdf", b"%PDF-1.4 data")
    assert response.status_code == 302
    rows = _rows(db, page)
    assert len(rows) == 1 and rows[0]["original_name"] == "report.pdf" and rows[0]["uploaded_by"] == editor["id"]
    assert rows[0]["filename"] != "report.pdf" and os.path.isfile(os.path.join(_folder(app), rows[0]["filename"]))
    html = client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert "report.pdf" in html and "Attachments (1)" in html
    download = client.get(f"/page/{page['slug']}/attachments/{rows[0]['id']}/download")
    assert download.status_code == 200 and download.data == b"%PDF-1.4 data"
    assert download.mimetype == "application/octet-stream"
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    download.close()


def test_api_upload_returns_json_and_editor_panel_lists_files(app, client, make_user, login, db):
    page = make_page(app, "Api page")
    login(client, make_user("att_api", role="editor"))
    response = _api_upload(client, page, "data.csv", b"a,b\n1,2\n")
    assert response.status_code == 201
    body = response.get_json()
    assert body["name"] == "data.csv" and body["size"] == 8
    assert body["download_url"].endswith(f"/attachments/{body['id']}/download")
    editor = client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    assert "data.csv" in editor and "data-attachments" in editor


def test_plain_users_cannot_upload_but_can_download(app, client, make_user, login, db, admin):
    page = make_page(app, "Shared")
    login(client, admin)
    _upload(client, page, "a.txt", b"A")
    client.post("/logout")
    login(client, make_user("att_reader"))
    assert _upload(client, page, "b.txt", b"B").status_code == 403
    assert _api_upload(client, page).status_code == 403
    attachment = _rows(db, page)[0]
    response = client.get(f"/page/{page['slug']}/attachments/{attachment['id']}/download")
    assert response.status_code == 200
    response.close()
    assert len(_rows(db, page)) == 1


def test_download_requires_attachment_view_permission(app, client, make_user, login, db, admin):
    page = make_page(app, "Private files")
    login(client, admin)
    _upload(client, page)
    client.post("/logout")
    reader = make_user("att_noview")
    restrict(db, reader, keys={"page.view_all"})
    login(client, reader)
    attachment = _rows(db, page)[0]
    assert client.get(f"/page/{page['slug']}/attachments/{attachment['id']}/download").status_code == 403
    assert client.get(f"/page/{page['slug']}/attachments/download-all").status_code == 403
    assert "notes.txt" not in client.get(f"/page/{page['slug']}").get_data(as_text=True)


def test_attachment_of_another_page_is_not_served(app, client, admin_client, db):
    first = make_page(app, "First")
    second = make_page(app, "Second")
    _upload(admin_client, first)
    attachment = _rows(db, first)[0]
    assert admin_client.get(f"/page/{second['slug']}/attachments/{attachment['id']}/download").status_code == 404
    assert admin_client.post(f"/page/{second['slug']}/attachments/{attachment['id']}/delete").status_code == 404
    assert len(_rows(db, first)) == 1


def test_restricted_category_hides_attachments(app, client, make_user, login, db, admin):
    secret = make_category(app, "Secret")
    open_cat = make_category(app, "Open")
    page = make_page(app, "Hidden doc", category_id=secret["id"])
    login(client, admin)
    _upload(client, page)
    client.post("/logout")
    editor = make_user("att_restricted", role="editor")
    restrict(db, editor, read=[open_cat["id"]], write=[open_cat["id"]])
    login(client, editor)
    attachment = _rows(db, page)[0]
    assert client.get(f"/page/{page['slug']}/attachments/{attachment['id']}/download").status_code == 404
    assert _api_upload(client, page).status_code == 404
    delete = client.delete(f"/api/attachments/{attachment['id']}", headers={"Accept": "application/json"})
    assert delete.status_code == 404
    assert len(_rows(db, page)) == 1


def test_editor_without_write_access_cannot_upload(app, client, make_user, login, db):
    cat = make_category(app, "Read only")
    page = make_page(app, "Read only page", category_id=cat["id"])
    editor = make_user("att_ro", role="editor")
    restrict(db, editor, read=[cat["id"]], write=[])
    login(client, editor)
    assert _upload(client, page).status_code == 403


def test_delete_own_versus_any(app, client, make_user, login, db):
    page = make_page(app, "Team")
    owner = make_user("att_owner", role="editor")
    other = make_user("att_other", role="editor")
    login(client, owner)
    _upload(client, page, "mine.txt")
    attachment = _rows(db, page)[0]
    client.post("/logout")
    login(client, other)
    assert client.post(f"/page/{page['slug']}/attachments/{attachment['id']}/delete").status_code == 403
    api = client.delete(f"/api/attachments/{attachment['id']}", headers={"Accept": "application/json"})
    assert api.status_code == 403
    restrict(db, other, keys={"page.edit_all", "attachment.delete_any"})
    assert client.post(f"/page/{page['slug']}/attachments/{attachment['id']}/delete").status_code == 302
    assert _rows(db, page) == []
    assert not os.path.exists(os.path.join(_folder(app), attachment["filename"]))


def test_owner_deletes_through_api(app, client, make_user, login, db):
    page = make_page(app, "Own delete")
    login(client, make_user("att_self", role="editor"))
    body = _api_upload(client, page).get_json()
    response = client.delete(body["delete_url"], headers={"Accept": "application/json"})
    assert response.status_code == 200 and response.get_json() == {"ok": True}
    assert _rows(db, page) == []


def test_upload_policy_and_size_limit(app, client, admin_client, db):
    page = make_page(app, "Policy")
    db.execute("UPDATE site_settings SET upload_mode = 'blacklist', upload_blacklist = 'exe'")
    assert _api_upload(admin_client, page, "tool.exe", b"MZ").status_code == 400
    db.execute("UPDATE site_settings SET upload_mode = 'whitelist', upload_whitelist = 'txt'")
    assert _api_upload(admin_client, page, "doc.pdf", b"x").status_code == 400
    assert _api_upload(admin_client, page, "ok.txt", b"x").status_code == 201
    db.execute("UPDATE site_settings SET upload_mode = 'allow_all', upload_max_size_mb = 1")
    big = _api_upload(admin_client, page, "big.txt", b"x" * (1024 * 1024 + 1))
    assert big.status_code == 413
    assert [r["original_name"] for r in _rows(db, page)] == ["ok.txt"]
    assert sorted(os.listdir(_folder(app))) == [_rows(db, page)[0]["filename"]]


def test_daily_upload_quota_applies(app, client, admin_client, db):
    page = make_page(app, "Quota")
    db.execute("UPDATE site_settings SET upload_quota_per_day_count = 1")
    assert _api_upload(admin_client, page, "one.txt").status_code == 201
    assert _api_upload(admin_client, page, "two.txt").status_code == 429
    assert len(_rows(db, page)) == 1


def test_download_all_zip_has_every_file_with_unique_names(app, client, admin_client, db):
    page = make_page(app, "Zip me")
    _upload(admin_client, page, "same.txt", b"one")
    _upload(admin_client, page, "same.txt", b"two")
    _upload(admin_client, page, "other.bin", b"\x00\x01")
    response = admin_client.get(f"/page/{page['slug']}/attachments/download-all")
    assert response.status_code == 200 and response.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        names = sorted(archive.namelist())
        assert names == ["other.bin", "same-2.txt", "same.txt"]
        assert {archive.read("same.txt"), archive.read("same-2.txt")} == {b"one", b"two"}
    response.close()


def test_download_all_without_attachments_redirects(app, admin_client):
    page = make_page(app, "Empty")
    assert admin_client.get(f"/page/{page['slug']}/attachments/download-all").status_code == 302


def test_legacy_blob_is_served_and_deleted(app, client, admin_client, db):
    page = make_page(app, "Legacy")
    blob_id = db.insert("file_blobs", {"filename": "0" * 32 + ".txt", "content": b"from blob", "file_size": 9})
    attachment_id = db.insert("page_attachments", {
        "page_id": page["id"], "filename": "0" * 32 + ".txt", "original_name": "old.txt", "file_size": 9,
        "blob_id": blob_id,
    })
    response = admin_client.get(f"/page/{page['slug']}/attachments/{attachment_id}/download")
    assert response.status_code == 200 and response.data == b"from blob"
    response.close()
    assert admin_client.post(f"/page/{page['slug']}/attachments/{attachment_id}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM file_blobs WHERE id = ?", (blob_id,)) == 0


def test_page_deletion_removes_files_and_blobs(app, client, admin_client, db):
    page = make_page(app, "Doomed")
    _upload(admin_client, page, "gone.txt")
    stored = _rows(db, page)[0]
    blob_id = db.insert("file_blobs", {"filename": "legacy", "content": b"x"})
    db.execute("UPDATE page_attachments SET blob_id = ? WHERE id = ?", (blob_id, stored["id"]))
    assert admin_client.post(f"/page/{page['slug']}/delete").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM page_attachments") == 0
    assert db.scalar("SELECT COUNT(*) FROM file_blobs") == 0
    assert not os.path.exists(os.path.join(_folder(app), stored["filename"]))


def test_cleanup_job_removes_orphans_but_keeps_recent_files(app, db):
    page = make_page(app, "Cleanup")
    folder = _folder(app)
    os.makedirs(folder, exist_ok=True)
    for name in ("old-orphan.txt", "fresh-orphan.txt", "known.txt"):
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(b"x")
    os.utime(os.path.join(folder, "old-orphan.txt"), (1, 1))
    os.utime(os.path.join(folder, "known.txt"), (1, 1))
    db.insert("page_attachments", {"page_id": page["id"], "filename": "known.txt", "original_name": "k.txt"})
    in_app(app, service.cleanup)
    assert sorted(os.listdir(folder)) == ["fresh-orphan.txt", "known.txt"]


def test_blocked_page_refuses_changes(app, client, make_user, login, db):
    page = make_page(app, "Frozen")
    db.execute("UPDATE pages SET pending_deletion = 1 WHERE id = ?", (page["id"],))
    admin = make_user("att_admin2", role="admin")
    login(client, admin)
    assert _upload(client, page).status_code == 403


def test_disabled_feature_hides_everything(app, client, admin_client, db):
    page = make_page(app, "Off")
    _upload(admin_client, page)
    attachment = _rows(db, page)[0]
    set_feature(app, "attachments", False)
    assert "Attachments" not in admin_client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert admin_client.get(f"/page/{page['slug']}/attachments/{attachment['id']}/download").status_code == 404
    assert _upload(admin_client, page).status_code == 404


def test_uploads_require_csrf(app, csrf_client, admin):
    from tests.conftest import PASSWORD, csrf_token_from

    page = make_page(app, "Csrf")
    token = csrf_token_from(csrf_client.get("/login"))
    csrf_client.post("/login", data={"username": admin["username"], "password": PASSWORD, "csrf_token": token})
    assert _upload(csrf_client, page).status_code == 400
    token = csrf_token_from(csrf_client.get(f"/page/{page['slug']}"))
    assert _upload(csrf_client, page, csrf_token=token).status_code == 302
