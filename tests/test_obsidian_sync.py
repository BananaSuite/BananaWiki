"""Tests for the experimental Obsidian sync helpers and CLI workflow."""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

import config


@pytest.fixture
def obsidian_env(tmp_path, monkeypatch):
    """Enable Obsidian sync and point asset folders at the temp directory."""
    upload_root = tmp_path / "uploads"
    attachment_root = tmp_path / "attachments"
    upload_root.mkdir()
    attachment_root.mkdir()
    monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", True)
    monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_root))
    monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(attachment_root))
    return {"uploads": upload_root, "attachments": attachment_root}


def _write_png(path):
    """Create a tiny valid PNG image at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color=(255, 215, 0)).save(path, format="PNG")


def test_obsidian_sync_requires_feature_flag(admin_user):
    """The experimental sync helpers must stay disabled unless the flag is on."""
    from helpers._obsidian_sync import authenticate_obsidian_user

    with pytest.raises(RuntimeError, match="disabled"):
        authenticate_obsidian_user("admin", "admin123")


def test_obsidian_sync_restricts_users_to_editor_roles(obsidian_env, admin_user):
    """Regular users must be denied even with valid credentials."""
    import db
    from helpers._obsidian_sync import authenticate_obsidian_user

    db.create_user("reader", generate_password_hash("reader123"), role="user")

    admin = authenticate_obsidian_user("admin", "admin123")
    assert admin["role"] == "admin"

    with pytest.raises(PermissionError, match="required permissions"):
        authenticate_obsidian_user("reader", "reader123")


def test_export_obsidian_vault_writes_markdown_assets_and_manifest(obsidian_env, admin_user, tmp_path):
    """Pulling a page should create a vault file, copied assets, and a manifest."""
    import db
    from helpers._obsidian_sync import export_obsidian_vault

    category_id = db.create_category("Guides")
    page_id = db.create_page("Sync Guide", "sync-guide", "Initial body", category_id, admin_user)

    _write_png(obsidian_env["uploads"] / "existing.png")
    attachment_path = obsidian_env["attachments"] / "manual.txt"
    attachment_path.write_text("manual", encoding="utf-8")
    attachment_id = db.add_page_attachment(page_id, "manual.txt", "manual.txt", attachment_path.stat().st_size, admin_user)

    content = (
        "Here is an image ![](/static/uploads/existing.png)\n\n"
        f"[Manual](/page/sync-guide/attachments/{attachment_id}/download)"
    )
    db.update_page(page_id, "Sync Guide", content, admin_user, "Prepare export")

    admin = db.get_user_by_id(admin_user)
    vault_dir = tmp_path / "vault"
    result = export_obsidian_vault(admin, vault_dir, slugs=["sync-guide"], include_home=False)

    page_file = vault_dir / "Guides" / "sync-guide.md"
    assert page_file.is_file()
    page_text = page_file.read_text(encoding="utf-8")
    assert "/static/uploads/existing.png" not in page_text
    assert "../assets/images/existing.png" in page_text
    assert "../assets/attachments/sync-guide" in page_text

    manifest = json.loads((vault_dir / ".bananawiki-obsidian.json").read_text(encoding="utf-8"))
    assert manifest["pages"][0]["slug"] == "sync-guide"
    assert (vault_dir / "assets" / "images" / "existing.png").is_file()
    assert any(item["vault_path"].startswith("assets/attachments/sync-guide/") for item in manifest["pages"][0]["attachments"])
    assert result["pages_exported"] == 1


def test_export_obsidian_vault_supports_directory_filters(obsidian_env, admin_user, tmp_path):
    """Selective pull should support category-directory filters."""
    import db
    from helpers._obsidian_sync import export_obsidian_vault

    guides_id = db.create_category("Guides")
    notes_id = db.create_category("Notes")
    db.create_page("Guide Page", "guide-page", "Guide body", guides_id, admin_user)
    db.create_page("Notes Page", "notes-page", "Notes body", notes_id, admin_user)

    admin = db.get_user_by_id(admin_user)
    vault_dir = tmp_path / "vault"
    export_obsidian_vault(admin, vault_dir, category_paths=["Guides"], include_home=False)

    assert (vault_dir / "Guides" / "guide-page.md").is_file()
    assert not (vault_dir / "Notes" / "notes-page.md").exists()


def test_import_obsidian_vault_updates_history_and_uploads_assets(obsidian_env, admin_user, tmp_path):
    """Pushing changes back should rewrite local asset refs and record page history."""
    import db
    from helpers._obsidian_sync import export_obsidian_vault, import_obsidian_vault

    category_id = db.create_category("Guides")
    page_id = db.create_page("Sync Guide", "sync-guide", "Original body", category_id, admin_user)

    _write_png(obsidian_env["uploads"] / "existing.png")
    db.update_page(
        page_id,
        "Sync Guide",
        "Original body\n\n![](/static/uploads/existing.png)",
        admin_user,
        "Prepare sync",
    )

    admin = db.get_user_by_id(admin_user)
    vault_dir = tmp_path / "vault"
    export_obsidian_vault(admin, vault_dir, slugs=["sync-guide"], include_home=False)

    _write_png(vault_dir / "assets" / "images" / "fresh.png")
    attachment_dir = vault_dir / "assets" / "attachments" / "sync-guide"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    (attachment_dir / "spec.txt").write_text("spec", encoding="utf-8")

    page_file = vault_dir / "Guides" / "sync-guide.md"
    page_file.write_text(
        "---\n"
        f"bananawiki_page_id: {page_id}\n"
        'bananawiki_slug: "sync-guide"\n'
        'bananawiki_category: "Guides"\n'
        "bananawiki_is_home: false\n"
        'title: "Sync Guide"\n'
        "---\n"
        "Updated body with local assets.\n\n"
        "![](../assets/images/fresh.png)\n\n"
        "[Spec](../assets/attachments/sync-guide/spec.txt)\n",
        encoding="utf-8",
    )

    result = import_obsidian_vault(admin, vault_dir, slugs=["sync-guide"])

    updated = db.get_page(page_id)
    assert "Updated body with local assets." in updated["content"]
    assert "/static/uploads/" in updated["content"]
    assert "/page/sync-guide/attachments/" in updated["content"]
    history = db.get_page_history(page_id)
    assert history[0]["edit_message"] == "Obsidian sync push"
    assert result["images_uploaded"] == 1
    assert result["attachments_uploaded"] >= 1


def test_import_obsidian_vault_can_create_new_pages_and_categories(obsidian_env, admin_user, tmp_path):
    """Admin pushes should be able to create missing category folders and new pages."""
    import db
    from helpers._obsidian_sync import import_obsidian_vault

    admin = db.get_user_by_id(admin_user)
    vault_dir = tmp_path / "vault"
    new_page = vault_dir / "Projects" / "release-plan.md"
    new_page.parent.mkdir(parents=True, exist_ok=True)
    new_page.write_text("# Release Plan\n\nShip it.\n", encoding="utf-8")

    result = import_obsidian_vault(admin, vault_dir, category_paths=["Projects"])

    created = db.get_page_by_slug("release-plan")
    assert created is not None
    category = db.get_category(created["category_id"])
    assert category["name"] == "Projects"
    assert result["pages_created"] == 1


# ---------------------------------------------------------------------------
# Edge cases for private helper functions
# ---------------------------------------------------------------------------

class TestSanitizeVaultSegment:
    """Unit tests for _sanitize_vault_segment."""

    def _call(self, value, fallback="fallback"):
        from helpers._obsidian_sync import _sanitize_vault_segment
        return _sanitize_vault_segment(value, fallback)

    def test_normal_value_passes_through(self):
        assert self._call("My Category") == "My Category"

    def test_backslash_replaced_with_dash(self):
        assert self._call("a\\b") == "a-b"

    def test_forward_slash_replaced_with_dash(self):
        assert self._call("a/b") == "a-b"

    def test_non_printable_chars_stripped(self):
        assert self._call("ab\x00cd") == "abcd"

    def test_leading_trailing_spaces_stripped(self):
        assert self._call("  hello  ") == "hello"

    def test_leading_trailing_dots_stripped(self):
        assert self._call("...hello...") == "hello"

    def test_empty_string_returns_fallback(self):
        assert self._call("") == "fallback"

    def test_only_spaces_returns_fallback(self):
        assert self._call("   ") == "fallback"

    def test_only_dots_returns_fallback(self):
        assert self._call("...") == "fallback"

    def test_none_returns_fallback(self):
        assert self._call(None) == "fallback"

    def test_unicode_allowed(self):
        result = self._call("Über-Guide")
        assert result == "Über-Guide"


class TestSplitFrontmatter:
    """Unit tests for _split_frontmatter."""

    def _call(self, text):
        from helpers._obsidian_sync import _split_frontmatter
        return _split_frontmatter(text)

    def test_no_frontmatter_returns_empty_dict_and_full_body(self):
        fm, body = self._call("Hello world")
        assert fm == {}
        assert body == "Hello world"

    def test_unclosed_frontmatter_treated_as_no_frontmatter(self):
        text = "---\ntitle: foo\n"
        fm, body = self._call(text)
        assert fm == {}
        assert body == text

    def test_valid_frontmatter_parsed(self):
        text = "---\ntitle: My Page\nbananawiki_slug: \"my-page\"\n---\nBody here."
        fm, body = self._call(text)
        assert fm["title"] == "My Page"
        assert fm["bananawiki_slug"] == "my-page"
        assert body == "Body here."

    def test_json_boolean_in_frontmatter(self):
        text = "---\nbananawiki_is_home: false\n---\nbody"
        fm, body = self._call(text)
        assert fm["bananawiki_is_home"] is False

    def test_json_integer_in_frontmatter(self):
        text = "---\nbananawiki_page_id: 42\n---\nbody"
        fm, body = self._call(text)
        assert fm["bananawiki_page_id"] == 42

    def test_multiple_colons_in_value(self):
        text = "---\nurl: http://example.com/path\n---\nbody"
        fm, body = self._call(text)
        assert fm["url"] == "http://example.com/path"

    def test_line_without_colon_skipped(self):
        text = "---\nno_colon_here\ntitle: Good\n---\nbody"
        fm, body = self._call(text)
        assert "no_colon_here" not in fm
        assert fm["title"] == "Good"

    def test_empty_key_skipped(self):
        text = "---\n: value\ntitle: Real\n---\nbody"
        fm, body = self._call(text)
        assert "" not in fm
        assert fm["title"] == "Real"

    def test_empty_frontmatter_block(self):
        # An empty frontmatter block "---\n---\nbody" is NOT recognized as valid
        # frontmatter because the closing "---" marker must be preceded by "\n"
        # from a position after the opening sequence; the whole text is returned
        # as the body with no frontmatter parsed.
        text = "---\n---\nbody"
        fm, body = self._call(text)
        assert fm == {}
        assert body == text


class TestResolvePageTitle:
    """Unit tests for _resolve_page_title."""

    def _call(self, body, frontmatter=None, entry=None, path_stem="my-page"):
        from helpers._obsidian_sync import _resolve_page_title
        from pathlib import PurePosixPath
        return _resolve_page_title(body, frontmatter or {}, entry, PurePosixPath(path_stem))

    def test_title_from_frontmatter(self):
        title = self._call("Body text", frontmatter={"title": "FM Title"})
        assert title == "FM Title"

    def test_title_from_manifest_entry(self):
        title = self._call("Body text", entry={"title": "Entry Title"})
        assert title == "Entry Title"

    def test_title_from_h1_in_body(self):
        title = self._call("# My Heading\n\nBody")
        assert title == "My Heading"

    def test_h1_with_extra_spaces(self):
        title = self._call("#   Trimmed   \n\nBody")
        assert title == "Trimmed"

    def test_title_from_stem_when_no_h1(self):
        title = self._call("No heading here.", path_stem="my-page")
        assert title == "my page"

    def test_empty_body_and_stem_returns_untitled(self):
        from helpers._obsidian_sync import _resolve_page_title
        from pathlib import PurePosixPath
        title = _resolve_page_title("", {}, None, PurePosixPath(""))
        assert title == "Untitled"

    def test_frontmatter_title_takes_precedence_over_h1(self):
        title = self._call("# H1 Title\n\nBody", frontmatter={"title": "FM Title"})
        assert title == "FM Title"


class TestNormalizeCategorySelector:
    """Unit tests for _normalize_category_selector."""

    def _call(self, path):
        from helpers._obsidian_sync import _normalize_category_selector
        return _normalize_category_selector(path)

    def test_empty_string_returns_empty(self):
        assert self._call("") == ""

    def test_dot_returns_empty(self):
        assert self._call(".") == ""

    def test_leading_slash_stripped(self):
        assert self._call("/Guides") == "Guides"

    def test_trailing_slash_stripped(self):
        assert self._call("Guides/") == "Guides"

    def test_nested_path_preserved(self):
        assert self._call("Guides/Subdir") == "Guides/Subdir"

    def test_dot_segments_removed(self):
        # "." segments within the path are skipped
        result = self._call("Guides/./Subdir")
        assert "." not in result.split("/")


class TestAttachmentIdFromRef:
    """Unit tests for _attachment_id_from_ref."""

    def _call(self, ref):
        from helpers._obsidian_sync import _attachment_id_from_ref
        return _attachment_id_from_ref(ref)

    def test_valid_ref_returns_id(self):
        assert self._call("/page/my-page/attachments/42/download") == 42

    def test_none_returns_none(self):
        assert self._call(None) is None

    def test_empty_string_returns_none(self):
        assert self._call("") is None

    def test_malformed_ref_returns_none(self):
        assert self._call("/page/my-page/download") is None


class TestLoadManifest:
    """Unit tests for _load_manifest."""

    def _call(self, path):
        from helpers._obsidian_sync import _load_manifest
        return _load_manifest(path)

    def test_missing_file_returns_empty_manifest(self, tmp_path):
        result = self._call(tmp_path / "nonexistent.json")
        assert result["version"] == 1
        assert result["pages"] == []

    def test_valid_manifest_loaded(self, tmp_path):
        manifest_path = tmp_path / ".bananawiki-obsidian.json"
        data = {"version": 1, "pages": [{"slug": "test"}]}
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        result = self._call(manifest_path)
        assert result["pages"][0]["slug"] == "test"

    def test_wrong_version_raises_value_error(self, tmp_path):
        manifest_path = tmp_path / ".bananawiki-obsidian.json"
        manifest_path.write_text(json.dumps({"version": 999, "pages": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="version"):
            self._call(manifest_path)


class TestAuthenticateObsidianUserEdgeCases:
    """Edge cases for authenticate_obsidian_user."""

    @pytest.fixture(autouse=True)
    def enable_sync(self, monkeypatch):
        monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", True)

    def test_wrong_password_raises_permission_error(self, admin_user):
        from helpers._obsidian_sync import authenticate_obsidian_user
        with pytest.raises(PermissionError, match="Invalid username or password"):
            authenticate_obsidian_user("admin", "wrongpassword")

    def test_nonexistent_user_raises_permission_error(self):
        from helpers._obsidian_sync import authenticate_obsidian_user
        with pytest.raises(PermissionError, match="Invalid username or password"):
            authenticate_obsidian_user("nosuchuser", "password123")

    def test_suspended_user_rejected(self, admin_user):
        import db
        from helpers._obsidian_sync import authenticate_obsidian_user
        db.update_user(admin_user, suspended=1)
        with pytest.raises(PermissionError, match="suspended"):
            authenticate_obsidian_user("admin", "admin123")

    def test_empty_username_rejected(self):
        from helpers._obsidian_sync import authenticate_obsidian_user
        with pytest.raises(PermissionError, match="Invalid username or password"):
            authenticate_obsidian_user("", "password123")

    def test_none_username_rejected(self):
        from helpers._obsidian_sync import authenticate_obsidian_user
        with pytest.raises(PermissionError, match="Invalid username or password"):
            authenticate_obsidian_user(None, "password123")


class TestFileHelpers:
    """Tests for _file_sha256 and _posix_relpath."""

    def test_file_sha256_matches_expected(self, tmp_path):
        from helpers._obsidian_sync import _file_sha256
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert _file_sha256(f) == expected

    def test_posix_relpath_basic(self, tmp_path):
        from helpers._obsidian_sync import _posix_relpath
        target = str(tmp_path / "a" / "b.txt")
        start = str(tmp_path / "a")
        result = _posix_relpath(target, start)
        assert result == "b.txt"

    def test_posix_relpath_parent(self, tmp_path):
        from helpers._obsidian_sync import _posix_relpath
        target = str(tmp_path / "images" / "img.png")
        start = str(tmp_path / "pages")
        result = _posix_relpath(target, start)
        assert result == "../images/img.png"

    def test_posix_relpath_uses_forward_slashes(self, tmp_path):
        from helpers._obsidian_sync import _posix_relpath
        target = str(tmp_path / "a" / "b" / "c.txt")
        start = str(tmp_path)
        result = _posix_relpath(target, start)
        assert "\\" not in result


class TestUniqueSlug:
    """Tests for _unique_slug."""

    def test_unique_slug_with_no_existing_page(self, admin_user):
        import db
        from helpers._obsidian_sync import _unique_slug
        db.update_site_settings(setup_done=1)
        slug = _unique_slug("my-new-page")
        assert slug == "my-new-page"

    def test_unique_slug_avoids_collision(self, admin_user):
        import db
        from helpers._obsidian_sync import _unique_slug
        cat_id = db.create_category("Test")
        db.create_page("Existing", "my-page", "body", cat_id, admin_user)
        slug = _unique_slug("my-page")
        assert slug == "my-page-2"

    def test_unique_slug_increments_until_free(self, admin_user):
        import db
        from helpers._obsidian_sync import _unique_slug
        cat_id = db.create_category("Test")
        db.create_page("P1", "slug", "b", cat_id, admin_user)
        db.create_page("P2", "slug-2", "b", cat_id, admin_user)
        result = _unique_slug("slug")
        assert result == "slug-3"

    def test_unique_slug_empty_base_uses_untitled(self, admin_user):
        from helpers._obsidian_sync import _unique_slug
        result = _unique_slug("")
        assert result == "untitled"


class TestExportEdgeCases:
    """Edge cases for export_obsidian_vault."""

    @pytest.fixture
    def obsidian_env(self, tmp_path, monkeypatch):
        upload_root = tmp_path / "uploads"
        attachment_root = tmp_path / "attachments"
        upload_root.mkdir()
        attachment_root.mkdir()
        monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", True)
        monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_root))
        monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(attachment_root))
        return {"uploads": upload_root, "attachments": attachment_root}

    def test_export_empty_wiki_produces_empty_vault(self, obsidian_env, admin_user, tmp_path):
        """After removing all non-home pages, export produces an empty manifest."""
        import db
        from helpers._obsidian_sync import export_obsidian_vault
        # Delete any default non-home pages the schema seeds (e.g. 'About')
        _, uncategorized = db.get_category_tree()
        for page in uncategorized:
            if not page["is_home"]:
                db.delete_page(page["id"])
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        result = export_obsidian_vault(admin, vault_dir, include_home=False)
        assert result["pages_exported"] == 0
        manifest = json.loads((vault_dir / ".bananawiki-obsidian.json").read_text(encoding="utf-8"))
        assert manifest["pages"] == []

    def test_export_with_unknown_slug_exports_nothing(self, obsidian_env, admin_user, tmp_path):
        """Requesting a slug that doesn't exist exports nothing."""
        import db
        from helpers._obsidian_sync import export_obsidian_vault
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        result = export_obsidian_vault(admin, vault_dir, slugs=["nonexistent"], include_home=False)
        assert result["pages_exported"] == 0

    def test_export_uncategorized_page(self, obsidian_env, admin_user, tmp_path):
        """Pages without a category should be exported to the vault root."""
        import db
        from helpers._obsidian_sync import export_obsidian_vault
        db.create_page("Orphan", "orphan", "No category", None, admin_user)
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        result = export_obsidian_vault(admin, vault_dir, slugs=["orphan"], include_home=False)
        assert result["pages_exported"] == 1
        assert (vault_dir / "orphan.md").is_file()

    def test_export_requires_feature_flag(self, admin_user, tmp_path, monkeypatch):
        """export_obsidian_vault raises when the feature flag is off."""
        import db
        from helpers._obsidian_sync import export_obsidian_vault
        monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", False)
        admin = db.get_user_by_id(admin_user)
        with pytest.raises(RuntimeError, match="disabled"):
            export_obsidian_vault(admin, tmp_path / "vault")


class TestImportEdgeCases:
    """Edge cases for import_obsidian_vault."""

    @pytest.fixture
    def obsidian_env(self, tmp_path, monkeypatch):
        upload_root = tmp_path / "uploads"
        attachment_root = tmp_path / "attachments"
        upload_root.mkdir()
        attachment_root.mkdir()
        monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", True)
        monkeypatch.setattr(config, "UPLOAD_FOLDER", str(upload_root))
        monkeypatch.setattr(config, "ATTACHMENT_FOLDER", str(attachment_root))
        return {"uploads": upload_root, "attachments": attachment_root}

    def test_import_empty_vault_is_no_op(self, obsidian_env, admin_user, tmp_path):
        """An empty vault directory should be gracefully handled."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        result = import_obsidian_vault(admin, vault_dir)
        assert result["pages_updated"] == 0
        assert result["pages_created"] == 0

    def test_import_updates_existing_page_by_slug(self, obsidian_env, admin_user, tmp_path):
        """Import should update a page identified by slug in the frontmatter."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        cat_id = db.create_category("Guides")
        page_id = db.create_page("My Guide", "my-guide", "Old content", cat_id, admin_user)
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        page_file = vault_dir / "Guides" / "my-guide.md"
        page_file.parent.mkdir(parents=True, exist_ok=True)
        page_file.write_text(
            '---\nbananawiki_slug: "my-guide"\ntitle: "My Guide"\n---\nNew content here.\n',
            encoding="utf-8",
        )
        result = import_obsidian_vault(admin, vault_dir)
        updated = db.get_page(page_id)
        assert "New content here." in updated["content"]
        assert result["pages_updated"] == 1

    def test_import_skips_assets_directory(self, obsidian_env, admin_user, tmp_path):
        """Markdown files inside assets/ should not be treated as pages."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        assets_md = vault_dir / "assets" / "note.md"
        assets_md.parent.mkdir(parents=True, exist_ok=True)
        assets_md.write_text("# Should not be imported\n", encoding="utf-8")
        result = import_obsidian_vault(admin, vault_dir)
        assert result["pages_created"] == 0

    def test_import_skips_hidden_directories(self, obsidian_env, admin_user, tmp_path):
        """Markdown files inside hidden directories (.obsidian etc) should be skipped."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        hidden_md = vault_dir / ".obsidian" / "config.md"
        hidden_md.parent.mkdir(parents=True, exist_ok=True)
        hidden_md.write_text("# Hidden\n", encoding="utf-8")
        result = import_obsidian_vault(admin, vault_dir)
        assert result["pages_created"] == 0

    def test_import_requires_feature_flag(self, admin_user, tmp_path, monkeypatch):
        """import_obsidian_vault raises when the feature flag is off."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        monkeypatch.setattr(config, "EXPERIMENTAL_OBSIDIAN_SYNC", False)
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        with pytest.raises(RuntimeError, match="disabled"):
            import_obsidian_vault(admin, vault_dir)

    def test_import_uses_h1_as_title_for_new_page(self, obsidian_env, admin_user, tmp_path):
        """When creating a new page, use the H1 heading as the title."""
        import db
        from helpers._obsidian_sync import import_obsidian_vault
        admin = db.get_user_by_id(admin_user)
        vault_dir = tmp_path / "vault"
        page_file = vault_dir / "h1-page.md"
        page_file.parent.mkdir(parents=True, exist_ok=True)
        page_file.write_text("# My H1 Title\n\nSome body.", encoding="utf-8")
        result = import_obsidian_vault(admin, vault_dir)
        created = db.get_page_by_slug("h1-page")
        assert created is not None
        assert created["title"] == "My H1 Title"
        assert result["pages_created"] == 1


class TestResolveAssetReference:
    """Tests for _resolve_asset_reference: path traversal prevention."""

    def test_valid_file_inside_vault(self, tmp_path):
        from helpers._obsidian_sync import _resolve_asset_reference
        vault_root = tmp_path / "vault"
        page_dir = vault_root / "pages"
        asset = vault_root / "assets" / "img.png"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"PNG")
        result = _resolve_asset_reference(vault_root.resolve(), page_dir, "../assets/img.png")
        assert result is not None
        assert result.name == "img.png"

    def test_path_traversal_outside_vault_rejected(self, tmp_path):
        from helpers._obsidian_sync import _resolve_asset_reference
        vault_root = tmp_path / "vault"
        page_dir = vault_root / "pages"
        vault_root.mkdir(parents=True)
        # Try to escape the vault with path traversal
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("sensitive", encoding="utf-8")
        result = _resolve_asset_reference(vault_root.resolve(), page_dir, "../../secret.txt")
        assert result is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        from helpers._obsidian_sync import _resolve_asset_reference
        vault_root = tmp_path / "vault"
        page_dir = vault_root / "pages"
        vault_root.mkdir(parents=True)
        result = _resolve_asset_reference(vault_root.resolve(), page_dir, "assets/missing.png")
        assert result is None
