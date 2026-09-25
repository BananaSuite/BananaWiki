"""Tests for the built-in wiki documentation spawning feature."""

import pytest

import db


class TestSpawnWikiDocs:
    """Test the core spawn_wiki_docs / is_docs_category functions."""

    def test_spawn_full_docs(self, admin_user):
        """Full documentation creates the expected category and pages."""
        cat_id = db.spawn_wiki_docs(admin_user, simplified=False)
        assert cat_id is not None
        cat = db.get_category(cat_id)
        assert cat is not None
        assert cat["name"] == db.DOCS_CATEGORY_NAME
        pages = db.get_pages_in_category(cat_id)
        assert len(pages) == 12

    def test_spawn_simplified_docs(self, admin_user):
        """Simplified documentation creates fewer pages."""
        cat_id = db.spawn_wiki_docs(admin_user, simplified=True)
        pages = db.get_pages_in_category(cat_id)
        assert len(pages) == 3

    def test_spawn_italian_docs_labels_pages(self, admin_user):
        """Italian docs mode should use native Italian page content."""
        cat_id = db.spawn_wiki_docs(admin_user, simplified=True, language="it")
        pages = db.get_pages_in_category(cat_id)
        assert len(pages) == 3
        assert any("Benvenuto" in p["title"] for p in pages)
        welcome = next(p for p in pages if p["slug"] == "bananawiki-welcome")
        assert "Per iniziare" in welcome["content"]
        assert "Three things you can do right now" not in welcome["content"]

    def test_spawned_docs_are_system_authored(self, admin_user):
        """Spawned documentation pages and history are attributed to the system."""
        cat_id = db.spawn_wiki_docs(admin_user, simplified=True)
        page = db.get_pages_in_category(cat_id)[0]
        assert str(page["last_edited_by"]) == str(db.SYSTEM_USER_ID)
        history = db.get_page_history(page["id"])
        assert history
        assert history[-1]["username"] == "the system"

    def test_respawn_replaces_existing(self, admin_user):
        """Respawning deletes old docs and creates fresh ones."""
        cat_id_old = db.spawn_wiki_docs(admin_user, simplified=False)
        cat_id_new = db.spawn_wiki_docs(admin_user, simplified=True)
        # Old category should be gone
        assert db.get_category(cat_id_old) is None
        # New category should exist with simplified pages
        pages = db.get_pages_in_category(cat_id_new)
        assert len(pages) == 3

    def test_is_docs_category(self, admin_user):
        """is_docs_category correctly identifies the tracked category."""
        cat_id = db.spawn_wiki_docs(admin_user)
        assert db.is_docs_category(cat_id) is True
        assert db.is_docs_category(999) is False
        assert db.is_docs_category(None) is False

    def test_docs_category_id_tracked_in_settings(self, admin_user):
        """Spawning stores the category ID in site settings."""
        cat_id = db.spawn_wiki_docs(admin_user)
        settings = db.get_site_settings()
        assert settings["docs_category_id"] == cat_id

    def test_docs_bypass_deletion_slowdown_default(self):
        """The bypass flag defaults to enabled (1)."""
        settings = db.get_site_settings()
        assert settings["docs_bypass_deletion_slowdown"] == 1

    def test_toggle_bypass_flag(self):
        """The bypass flag can be toggled via update_site_settings."""
        db.update_site_settings(docs_bypass_deletion_slowdown=0)
        settings = db.get_site_settings()
        assert settings["docs_bypass_deletion_slowdown"] == 0
        db.update_site_settings(docs_bypass_deletion_slowdown=1)
        settings = db.get_site_settings()
        assert settings["docs_bypass_deletion_slowdown"] == 1

    def test_spawn_docs_page_slugs_are_unique(self, admin_user):
        """All spawned page slugs must be unique."""
        cat_id = db.spawn_wiki_docs(admin_user)
        pages = db.get_pages_in_category(cat_id)
        slugs = [p["slug"] for p in pages]
        assert len(slugs) == len(set(slugs))

    def test_clearing_docs_category_id(self, admin_user):
        """Setting docs_category_id to None clears the tracking."""
        db.spawn_wiki_docs(admin_user)
        db.update_site_settings(docs_category_id=None)
        settings = db.get_site_settings()
        assert settings["docs_category_id"] is None
        assert db.is_docs_category(1) is False

    def test_spawn_when_category_already_deleted(self, admin_user):
        """If the tracked category was manually deleted, respawn works cleanly."""
        cat_id = db.spawn_wiki_docs(admin_user)
        # Manually delete the category (simulating user action)
        db.delete_category(cat_id, page_action="delete")
        # Respawn should work without error
        new_cat_id = db.spawn_wiki_docs(admin_user)
        assert new_cat_id is not None
        pages = db.get_pages_in_category(new_cat_id)
        assert len(pages) == 12


class TestSpawnDocsRoute:
    """Test the admin spawn-docs route."""

    def test_spawn_docs_requires_admin(self, client, editor_user):
        """Non-admin users cannot spawn documentation."""
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.post("/admin/spawn-docs")
        assert resp.status_code in (302, 403)

    def test_spawn_docs_as_admin(self, logged_in_admin):
        """Admin can spawn documentation."""
        resp = logged_in_admin.post("/admin/spawn-docs", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Documentation has been successfully created" in resp.data

    def test_spawn_docs_creates_category(self, logged_in_admin):
        """Spawning creates the BananaWiki category with pages."""
        logged_in_admin.post("/admin/spawn-docs")
        settings = db.get_site_settings()
        cat_id = settings["docs_category_id"]
        assert cat_id is not None
        pages = db.get_pages_in_category(cat_id)
        assert len(pages) > 0

    def test_spawn_docs_simplified(self, logged_in_admin):
        """Spawning with simplified=1 creates fewer pages."""
        logged_in_admin.post("/admin/spawn-docs", data={"simplified": "1"})
        settings = db.get_site_settings()
        cat_id = settings["docs_category_id"]
        pages = db.get_pages_in_category(cat_id)
        assert len(pages) == 3

    def test_spawn_docs_italian_language(self, logged_in_admin):
        """Admin spawn docs should support Italian labels."""
        logged_in_admin.post("/admin/spawn-docs", data={"simplified": "1", "docs_language": "it"})
        settings = db.get_site_settings()
        pages = db.get_pages_in_category(settings["docs_category_id"])
        assert any("Benvenuto" in p["title"] for p in pages)

    def test_respawn_docs_replaces(self, logged_in_admin):
        """Respawning replaces existing documentation."""
        logged_in_admin.post("/admin/spawn-docs")
        settings = db.get_site_settings()
        old_cat = settings["docs_category_id"]
        logged_in_admin.post("/admin/spawn-docs", data={"simplified": "1"})
        settings = db.get_site_settings()
        new_cat = settings["docs_category_id"]
        assert new_cat != old_cat
        assert db.get_category(old_cat) is None


class TestDocsSettingsRoute:
    """Test the admin docs settings route."""

    def test_update_bypass_flag(self, logged_in_admin):
        """Admin can toggle the bypass deletion slowdown flag."""
        # Disable
        logged_in_admin.post("/admin/docs-settings", data={})
        settings = db.get_site_settings()
        assert settings["docs_bypass_deletion_slowdown"] == 0
        # Enable
        logged_in_admin.post("/admin/docs-settings",
                             data={"docs_bypass_deletion_slowdown": "1"})
        settings = db.get_site_settings()
        assert settings["docs_bypass_deletion_slowdown"] == 1


class TestSetupDocsOption:
    """Test that the setup page spawns documentation when requested."""

    def test_setup_without_docs(self, client):
        """Setup without spawn_docs does not create documentation."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
        })
        settings = db.get_site_settings()
        assert settings["docs_category_id"] is None
        assert settings["interface_language"] == "en"

    def test_setup_with_docs(self, client):
        """Setup with spawn_docs creates full documentation."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
            "spawn_docs": "1",
        })
        settings = db.get_site_settings()
        assert settings["docs_category_id"] is not None
        pages = db.get_pages_in_category(settings["docs_category_id"])
        assert len(pages) == 12

    def test_setup_with_simplified_docs(self, client):
        """Setup with simplified docs creates fewer pages."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
            "spawn_docs": "1",
            "simplified_docs": "1",
        })
        settings = db.get_site_settings()
        assert settings["docs_category_id"] is not None
        pages = db.get_pages_in_category(settings["docs_category_id"])
        assert len(pages) == 3

    def test_setup_can_set_italian_language(self, client):
        """Setup can set the default interface language to Italian."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
            "interface_language": "it",
        })
        settings = db.get_site_settings()
        assert settings["interface_language"] == "it"

    def test_setup_spawns_italian_docs(self, client):
        """Setup docs language option should spawn Italian-labelled docs."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
            "interface_language": "it",
            "spawn_docs": "1",
            "simplified_docs": "1",
            "docs_language": "it",
        })
        settings = db.get_site_settings()
        pages = db.get_pages_in_category(settings["docs_category_id"])
        assert any("Benvenuto" in p["title"] for p in pages)

    def test_setup_docs_language_can_differ_from_interface_language(self, client):
        """Setup should honor the docs language selector independently."""
        client.post("/setup", data={
            "username": "admin",
            "password": "admin123",
            "confirm_password": "admin123",
            "interface_language": "en",
            "spawn_docs": "1",
            "simplified_docs": "1",
            "docs_language": "it",
        })
        settings = db.get_site_settings()
        assert settings["interface_language"] == "en"
        pages = db.get_pages_in_category(settings["docs_category_id"])
        welcome = next(p for p in pages if p["slug"] == "bananawiki-welcome")
        assert "Per iniziare" in welcome["content"]
        assert "Three things you can do right now" not in welcome["content"]


class TestDocsBypassDeletionSlowdown:
    """Test that docs pages bypass deletion slowdown when the flag is set."""

    def _setup_docs_and_plugin(self, logged_in_admin):
        """Create docs and ensure deletion_slowdown plugin is enabled."""
        logged_in_admin.post("/admin/spawn-docs")
        settings = db.get_site_settings()
        return settings["docs_category_id"]

    def test_docs_page_bypasses_slowdown(self, logged_in_admin):
        """A docs page should be deleted immediately, not queued."""
        cat_id = self._setup_docs_and_plugin(logged_in_admin)
        pages = db.get_pages_in_category(cat_id)
        slug = pages[0]["slug"]
        # Ensure bypass is enabled
        db.update_site_settings(docs_bypass_deletion_slowdown=1)
        # Delete the page
        resp = logged_in_admin.post(f"/page/{slug}/delete", follow_redirects=True)
        assert resp.status_code == 200
        # Page should be gone (not pending)
        page = db.get_page_by_slug(slug)
        assert page is None

    def test_docs_page_respects_slowdown_when_bypass_off(self, logged_in_admin):
        """When bypass is disabled, docs pages should go through slowdown."""
        cat_id = self._setup_docs_and_plugin(logged_in_admin)
        pages = db.get_pages_in_category(cat_id)
        slug = pages[0]["slug"]
        # Disable bypass
        db.update_site_settings(docs_bypass_deletion_slowdown=0)
        # Delete the page (should be pending since deletion_slowdown is enabled)
        resp = logged_in_admin.post(f"/page/{slug}/delete", follow_redirects=True)
        assert resp.status_code == 200
        page = db.get_page_by_slug(slug)
        # Page should still exist with pending_deletion flag
        assert page is not None
        assert page["pending_deletion"] == 1

    def test_docs_category_delete_bypasses_slowdown(self, logged_in_admin):
        """Deleting the docs category with delete action should bypass slowdown."""
        cat_id = self._setup_docs_and_plugin(logged_in_admin)
        pages = db.get_pages_in_category(cat_id)
        page_ids = [p["id"] for p in pages]
        db.update_site_settings(docs_bypass_deletion_slowdown=1)
        # Delete category with page_action=delete
        resp = logged_in_admin.post(f"/category/{cat_id}/delete",
                                    data={"page_action": "delete"},
                                    follow_redirects=True)
        assert resp.status_code == 200
        # All pages should be gone
        for pid in page_ids:
            assert db.get_page(pid) is None
        # docs_category_id should be cleared
        settings = db.get_site_settings()
        assert settings["docs_category_id"] is None


class TestDocsSettingsTemplate:
    """Test that the admin settings page shows documentation controls."""

    def test_settings_shows_spawn_button(self, logged_in_admin):
        """The settings page should show the spawn documentation button."""
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200
        assert b"Spawn Documentation" in resp.data or b"Respawn Documentation" in resp.data

    def test_settings_shows_bypass_checkbox(self, logged_in_admin):
        """The settings page should show the bypass deletion slowdown checkbox."""
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200
        assert b"docs_bypass_deletion_slowdown" in resp.data

    def test_settings_shows_respawn_after_spawn(self, logged_in_admin):
        """After spawning, the button text should change to 'Respawn'."""
        logged_in_admin.post("/admin/spawn-docs")
        resp = logged_in_admin.get("/global-settings")
        assert b"Respawn Documentation" in resp.data


class TestBuildDocsArchive:
    """``db.build_docs_archive`` produces a re-importable Markdown ZIP."""

    def _read_zip(self, payload):
        import io
        import zipfile

        return zipfile.ZipFile(io.BytesIO(payload))

    def test_full_en_archive_matches_spawn_pages(self, admin_user):
        cat_id = db.spawn_wiki_docs(admin_user, simplified=False, language="en")
        pages = db.get_pages_in_category(cat_id)
        zip_bytes, filename = db.build_docs_archive(simplified=False, language="en")
        assert filename == "bananawiki-docs-full-en.zip"
        with self._read_zip(zip_bytes) as zf:
            names = zf.namelist()
        # One markdown file per page, all under the BananaWiki/ folder.
        assert len(names) == len(pages)
        assert all(name.startswith("BananaWiki/") and name.endswith(".md") for name in names)

    def test_simplified_it_archive_is_natively_translated(self):
        zip_bytes, filename = db.build_docs_archive(simplified=True, language="it")
        assert filename == "bananawiki-docs-simplified-it.zip"
        with self._read_zip(zip_bytes) as zf:
            welcome = zf.read("BananaWiki/bananawiki-welcome.md").decode("utf-8")
        # Native Italian, not English with a header notice.
        assert "Benvenuto in BananaWiki" in welcome
        assert "Per iniziare" in welcome
        # The English heading must not leak through.
        assert "Quick Start" not in welcome


class TestAdminDownloadDocsRoute:
    """Admin download-docs HTTP route."""

    def test_admin_download_full_en(self, client, logged_in_admin):
        resp = client.get("/admin/download-docs")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("application/zip")
        assert "bananawiki-docs-full-en.zip" in resp.headers.get(
            "Content-Disposition", "",
        )

    def test_admin_download_simplified_it(self, client, logged_in_admin):
        resp = client.get("/admin/download-docs?simplified=1&docs_language=it")
        assert resp.status_code == 200
        assert "bananawiki-docs-simplified-it.zip" in resp.headers.get(
            "Content-Disposition", "",
        )

    def test_non_admin_cannot_download(self, client, logged_in_user):
        resp = client.get("/admin/download-docs")
        assert resp.status_code in (302, 403)


class TestSetupTemplate:
    """Test that the setup page shows the documentation option."""

    def test_setup_shows_docs_option(self, client):
        """The setup page should show the docs checkbox."""
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert b"spawn_docs" in resp.data
        assert b"built-in documentation" in resp.data.lower()

    def test_setup_shows_simplified_option(self, client):
        """The setup page should show the simplified docs checkbox."""
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert b"simplified_docs" in resp.data
        assert b'name="simplified_docs" value="1" disabled' in resp.data

    def test_setup_shows_interface_language_option(self, client):
        """The setup page offers each interface language as a labelled choice.

        The badge carries the language code rather than a flag: regional
        indicator characters need a colour emoji font that stock servers and
        many Linux desktops lack, and a flag does not identify a language.
        """
        resp = client.get("/setup")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'type="radio" name="interface_language" value="en"' in html
        assert 'type="radio" name="interface_language" value="it"' in html
        assert '<span class="setup-language-flag" aria-hidden="true">EN</span>' in html
        assert '<span class="setup-language-flag" aria-hidden="true">IT</span>' in html
        assert b'<select name="docs_language"' in resp.data
        assert b"Documentation language" in resp.data


class TestDocsFilesInSync:
    """The on-disk ``docs/user-guide/`` mirror must match ``db._wiki_docs``."""

    def test_user_guide_files_match_spawn_content(self):
        """``scripts/sync_user_guide_docs.py`` should report no drift.

        The script regenerates ``docs/user-guide/<lang>/<slug>.md`` from
        the canonical Python source (``db._wiki_docs``).  When this test
        fails, run ``python scripts/sync_user_guide_docs.py`` and commit
        the resulting changes so users see the same content on disk and
        in the spawned ``BananaWiki`` category.
        """
        import os
        import sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts_dir = os.path.join(repo_root, "scripts")
        sys.path.insert(0, scripts_dir)
        try:
            import sync_user_guide_docs as sync_mod  # type: ignore  # noqa: E402

            expected = sync_mod.render_user_guide()
            drift = sync_mod._check_files(expected)
            existing = sync_mod._existing_user_guide_paths()
            expected_paths = {p.replace(os.sep, "/") for p in expected.keys()}
            unexpected = [p for p in existing if p not in expected_paths]
        finally:
            sys.path.remove(scripts_dir)

        assert not drift, (
            "docs/user-guide/ is out of date relative to db/_wiki_docs.py.  "
            "Run `python scripts/sync_user_guide_docs.py` and commit the "
            f"updates.  Stale files: {drift}"
        )
        assert not unexpected, (
            "docs/user-guide/ contains files that no longer correspond to "
            "any spawn page.  Run `python scripts/sync_user_guide_docs.py` "
            f"to clean them up.  Unexpected files: {unexpected}"
        )

    def test_user_guide_contains_native_italian(self):
        """The IT user guide on disk must use the native Italian rewrite."""
        import os

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        welcome_path = os.path.join(
            repo_root, "docs", "user-guide", "it", "bananawiki-welcome.md"
        )
        assert os.path.exists(welcome_path)
        with open(welcome_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "Benvenuto in BananaWiki" in content
        # English-only sentinel string must not leak through.
        assert "Welcome to BananaWiki" not in content
        assert "Quick Start" not in content
