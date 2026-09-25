"""
Tests for BananaWiki functionality not covered by the existing test suite.

Covers:
  - Page slug rename route (/page/<slug>/rename)
  - Page attachment download route (/page/<slug>/attachments/<id>/download)
  - Page attachment download-all route (/page/<slug>/attachments/download-all)
  - download_attachment returns 404 when file is missing from disk
  - download_all_attachments redirects when page has no attachments
  - rename_page_slug edge cases (home page, duplicate slug, same slug, invalid slug)
"""

import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a fresh temporary database and isolated folders for every test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config, "LOGGING_LEVEL", "off")
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(tmp_path / "attachments"))
    import db as db_mod
    db_mod.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def clear_rl_store():
    """Clear the in-memory rate-limit store before and after each test."""
    import app as app_mod
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()
    yield
    with app_mod._RL_LOCK:
        app_mod._RL_STORE.clear()


@pytest.fixture
def client():
    """Flask test client with CSRF disabled."""
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_user():
    """Create an admin user and mark setup as done."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("admin", generate_password_hash("admin123"), role="admin")
    db.update_site_settings(setup_done=1)
    return uid


@pytest.fixture
def editor_user(admin_user):
    """Create an editor user (setup already done by admin_user fixture)."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("editor1", generate_password_hash("editor123"), role="editor")
    return uid


@pytest.fixture
def regular_user(admin_user):
    """Create a regular (viewer) user."""
    from werkzeug.security import generate_password_hash
    import db
    uid = db.create_user("user1", generate_password_hash("user123"), role="user")
    return uid


@pytest.fixture
def logged_in_admin(client, admin_user):
    """Return a client logged in as admin."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture
def logged_in_editor(client, editor_user):
    """Return a client logged in as editor."""
    client.post("/login", data={"username": "editor1", "password": "editor123"})
    return client


@pytest.fixture
def logged_in_user(client, regular_user):
    """Return a client logged in as a regular user."""
    client.post("/login", data={"username": "user1", "password": "user123"})
    return client


# ---------------------------------------------------------------------------
# rename_page_slug: /page/<slug>/rename
# ---------------------------------------------------------------------------

class TestRenamePageSlug:
    """Tests for the rename_page_slug route."""

    def test_rename_page_success(self, logged_in_admin):
        """Admin can rename a non-home page to a new slug."""
        import db
        db.create_page("Rename Me", "rename-me", "content")
        resp = logged_in_admin.post(
            "/page/rename-me/rename",
            data={"new_slug": "new-name"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Page URL updated" in resp.data
        assert db.get_page_by_slug("new-name") is not None
        assert db.get_page_by_slug("rename-me") is None

    def test_rename_page_nonexistent_returns_404(self, logged_in_admin):
        """Renaming a page that does not exist returns 404."""
        resp = logged_in_admin.post(
            "/page/does-not-exist/rename",
            data={"new_slug": "something"},
        )
        assert resp.status_code == 404

    def test_rename_home_page_blocked(self, logged_in_admin):
        """Cannot rename the home page slug."""
        import db
        home = db.get_home_page()
        resp = logged_in_admin.post(
            f"/page/{home['slug']}/rename",
            data={"new_slug": "my-home"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Cannot change the URL of the home page" in resp.data
        # Slug unchanged
        assert db.get_home_page()["slug"] == home["slug"]

    def test_rename_page_empty_slug_shows_error(self, logged_in_admin):
        """Submitting an empty new_slug shows an error."""
        import db
        db.create_page("Empty Slug Test", "empty-slug-test", "content")
        resp = logged_in_admin.post(
            "/page/empty-slug-test/rename",
            data={"new_slug": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"New URL slug is required" in resp.data
        # Original slug still exists
        assert db.get_page_by_slug("empty-slug-test") is not None

    def test_rename_page_same_slug_shows_info(self, logged_in_admin):
        """Renaming a page to its current slug shows an info message."""
        import db
        db.create_page("Same Slug Test", "same-slug-test", "content")
        resp = logged_in_admin.post(
            "/page/same-slug-test/rename",
            data={"new_slug": "same-slug-test"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"same as the current one" in resp.data

    def test_rename_page_duplicate_slug_shows_error(self, logged_in_admin):
        """Renaming to an already-used slug shows an error."""
        import db
        db.create_page("Page One", "page-one", "content")
        db.create_page("Page Two", "page-two", "content")
        resp = logged_in_admin.post(
            "/page/page-one/rename",
            data={"new_slug": "page-two"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already in use" in resp.data
        # Both pages still exist with their original slugs
        assert db.get_page_by_slug("page-one") is not None
        assert db.get_page_by_slug("page-two") is not None

    def test_rename_page_invalid_slug_chars(self, logged_in_admin):
        """A slug made up entirely of non-slug characters falls back to 'page'.

        slugify('!!!') returns 'page' (the default fallback). Since no page
        with slug 'page' exists in a fresh DB, the rename should succeed and
        redirect to /page/page.
        """
        import db
        db.create_page("Invalid Slug Test", "invalid-slug-test", "content")
        # No page exists with slug "page", so slugify("!!!") → "page" succeeds
        resp = logged_in_admin.post(
            "/page/invalid-slug-test/rename",
            data={"new_slug": "!!!"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Page should now be accessible under slug "page"
        assert db.get_page_by_slug("page") is not None
        assert db.get_page_by_slug("invalid-slug-test") is None

    def test_rename_page_requires_editor(self, logged_in_user):
        """Regular users cannot rename page slugs."""
        import db
        db.create_page("Editor Only", "editor-only", "content")
        resp = logged_in_user.post(
            "/page/editor-only/rename",
            data={"new_slug": "new-editor"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not have permission" in resp.data
        # Slug unchanged
        assert db.get_page_by_slug("editor-only") is not None


# ---------------------------------------------------------------------------
# download_attachment: /page/<slug>/attachments/<id>/download
# ---------------------------------------------------------------------------

class TestPageAttachmentDownload:
    """Tests for the single-attachment download route."""

    def _create_attachment(self, tmp_path, page_id, user_id):
        """Helper: create a real file on disk and record it in the DB."""
        import db
        attach_dir = config.ATTACHMENT_FOLDER
        os.makedirs(attach_dir, exist_ok=True)
        stored_name = "test_file.txt"
        filepath = os.path.join(attach_dir, stored_name)
        with open(filepath, "w") as fh:
            fh.write("hello attachment")
        att_id = db.add_page_attachment(
            page_id, stored_name, "original.txt", 16, user_id
        )
        return att_id, filepath

    def test_download_attachment_success(self, logged_in_admin, admin_user, tmp_path):
        """Logged-in user can download an attachment that exists on disk."""
        import db
        page_id = db.create_page("Attach Page", "attach-page", "content")
        att_id, _ = self._create_attachment(tmp_path, page_id, admin_user)

        resp = logged_in_admin.get(f"/page/attach-page/attachments/{att_id}/download")
        assert resp.status_code == 200
        assert b"hello attachment" in resp.data

    def test_download_attachment_wrong_page_returns_404(
        self, logged_in_admin, admin_user, tmp_path
    ):
        """Attachment belongs to a different page → 404."""
        import db
        page_a = db.create_page("Page A", "page-a-dl", "content")
        db.create_page("Page B", "page-b-dl", "content")
        att_id, _ = self._create_attachment(tmp_path, page_a, admin_user)

        # Try to fetch the attachment via page_b's slug
        resp = logged_in_admin.get(f"/page/page-b-dl/attachments/{att_id}/download")
        assert resp.status_code == 404

    def test_download_attachment_nonexistent_returns_404(self, logged_in_admin):
        """Non-existent attachment ID returns 404."""
        import db
        db.create_page("Page No Att", "page-no-att", "content")
        resp = logged_in_admin.get("/page/page-no-att/attachments/99999/download")
        assert resp.status_code == 404

    def test_download_attachment_missing_file_returns_404(
        self, logged_in_admin, admin_user
    ):
        """If the file has been deleted from disk, the route returns 404."""
        import db
        page_id = db.create_page("Missing File Page", "missing-file-page", "content")
        # Record attachment in DB but do NOT create the file on disk
        att_id = db.add_page_attachment(
            page_id, "ghost.txt", "ghost.txt", 5, admin_user
        )
        resp = logged_in_admin.get(
            f"/page/missing-file-page/attachments/{att_id}/download"
        )
        assert resp.status_code == 404

    def test_download_attachment_requires_login(self, client, admin_user, tmp_path):
        """Unauthenticated requests are redirected to the login page."""
        import db
        page_id = db.create_page("Auth Test Page", "auth-test-page", "content")
        att_id, _ = self._create_attachment(tmp_path, page_id, admin_user)
        resp = client.get(f"/page/auth-test-page/attachments/{att_id}/download")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_download_attachment_respects_category_read_access(self, client, admin_user, tmp_path):
        """Restricted users cannot download attachments from categories they cannot view."""
        import db
        from helpers._permissions import get_default_permissions
        from werkzeug.security import generate_password_hash

        cat_id = db.create_category("Restricted Attachments")
        page_id = db.create_page("Secret Attachment", "secret-attachment", "content", category_id=cat_id)
        att_id, _ = self._create_attachment(tmp_path, page_id, admin_user)

        restricted_user = db.create_user("restricted_dl", generate_password_hash("pass123"), role="user")
        db.set_user_permissions(
            restricted_user,
            get_default_permissions("user"),
            read_restricted=True,
            read_category_ids=[],
        )

        client.post("/login", data={"username": "restricted_dl", "password": "pass123"})
        resp = client.get(f"/page/secret-attachment/attachments/{att_id}/download")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# download_all_attachments: /page/<slug>/attachments/download-all
# ---------------------------------------------------------------------------

class TestPageAttachmentDownloadAll:
    """Tests for the download-all attachments route."""

    def _create_attachment(self, page_id, user_id, filename="test.txt", content="data"):
        """Helper: create a file on disk and DB record."""
        import db
        attach_dir = config.ATTACHMENT_FOLDER
        os.makedirs(attach_dir, exist_ok=True)
        stored_name = filename
        filepath = os.path.join(attach_dir, stored_name)
        with open(filepath, "w") as fh:
            fh.write(content)
        return db.add_page_attachment(page_id, stored_name, filename, len(content), user_id)

    def test_download_all_returns_zip(self, logged_in_admin, admin_user):
        """Download-all returns a ZIP archive when attachments exist."""
        import db
        page_id = db.create_page("Multi Attach", "multi-attach", "content")
        self._create_attachment(page_id, admin_user, "a.txt", "aaa")
        self._create_attachment(page_id, admin_user, "b.txt", "bbb")

        resp = logged_in_admin.get("/page/multi-attach/attachments/download-all")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        names = zf.namelist()
        assert "a.txt" in names
        assert "b.txt" in names

    def test_download_all_no_attachments_redirects(self, logged_in_admin):
        """Download-all redirects with an error flash when there are no attachments."""
        import db
        db.create_page("Empty Attach Page", "empty-attach-page", "content")
        resp = logged_in_admin.get(
            "/page/empty-attach-page/attachments/download-all",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"No attachments" in resp.data

    def test_download_all_nonexistent_page_returns_404(self, logged_in_admin):
        """Download-all on a non-existent page returns 404."""
        resp = logged_in_admin.get("/page/no-such-page/attachments/download-all")
        assert resp.status_code == 404

    def test_download_all_requires_login(self, client, admin_user):
        """Unauthenticated requests are redirected to the login page."""
        import db
        db.create_page("Download Auth Page", "download-auth-page", "content")
        resp = client.get("/page/download-auth-page/attachments/download-all")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_download_all_respects_deindexed_visibility(self, logged_in_user, admin_user):
        """Users without deindexed-page permission cannot bulk-download hidden page attachments."""
        import db

        page_id = db.create_page(
            "Hidden Attachments",
            "hidden-attachments",
            "content",
        )
        db.set_page_deindexed(page_id, True)
        self._create_attachment(page_id, admin_user, "hidden.txt", "secret")

        resp = logged_in_user.get("/page/hidden-attachments/attachments/download-all")
        assert resp.status_code == 403
