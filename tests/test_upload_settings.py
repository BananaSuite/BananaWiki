"""Tests for the flexible upload settings system."""

import pytest

import db
from helpers._validation import (
    _safe_ext,
    _parse_ext_list,
    _is_extension_allowed_by_settings,
    _ALWAYS_BLOCKED_EXTENSIONS,
    allowed_attachment,
    allowed_chat_file,
    get_effective_max_upload_size,
)


class TestParseExtList:
    """Test the _parse_ext_list helper."""

    def test_empty_string(self):
        """Empty string returns empty set."""
        assert _parse_ext_list("") == frozenset()

    def test_none_input(self):
        """None input returns empty set."""
        assert _parse_ext_list(None) == frozenset()

    def test_simple_list(self):
        """Simple comma-separated list is parsed correctly."""
        result = _parse_ext_list("pdf, docx, png")
        assert result == frozenset({"pdf", "docx", "png"})

    def test_with_dots(self):
        """Leading dots are stripped."""
        result = _parse_ext_list(".pdf, .docx, .png")
        assert result == frozenset({"pdf", "docx", "png"})

    def test_whitespace_handling(self):
        """Extra whitespace is stripped."""
        result = _parse_ext_list("  pdf ,  docx  , png  ")
        assert result == frozenset({"pdf", "docx", "png"})

    def test_case_insensitive(self):
        """Extensions are lowercased."""
        result = _parse_ext_list("PDF, DOCX, Png")
        assert result == frozenset({"pdf", "docx", "png"})


class TestAlwaysBlockedExtensions:
    """Test that dangerous extensions are always blocked."""

    @pytest.mark.parametrize("ext", ["exe", "bat", "cmd", "dll", "scr", "vbs", "ps1"])
    def test_dangerous_ext_blocked_in_allow_all(self, ext):
        """Dangerous extensions are blocked even in allow_all mode."""
        settings = {"upload_mode": "allow_all"}
        assert _is_extension_allowed_by_settings(ext, settings) is False

    @pytest.mark.parametrize("ext", ["exe", "bat", "cmd", "dll"])
    def test_dangerous_ext_blocked_in_blacklist(self, ext):
        """Dangerous extensions are blocked even in blacklist mode."""
        settings = {"upload_mode": "blacklist", "upload_blacklist": ""}
        assert _is_extension_allowed_by_settings(ext, settings) is False


class TestIsExtensionAllowedBySettings:
    """Test the settings-aware extension check."""

    def test_whitelist_mode_with_custom_list(self):
        """Custom whitelist only allows listed extensions."""
        settings = {"upload_mode": "whitelist", "upload_whitelist": "pdf, txt"}
        assert _is_extension_allowed_by_settings("pdf", settings) is True
        assert _is_extension_allowed_by_settings("txt", settings) is True
        assert _is_extension_allowed_by_settings("docx", settings) is False

    def test_whitelist_mode_empty_uses_defaults(self):
        """Empty whitelist falls back to config defaults."""
        settings = {"upload_mode": "whitelist", "upload_whitelist": ""}
        # pdf is in config.ATTACHMENT_ALLOWED_EXTENSIONS
        assert _is_extension_allowed_by_settings("pdf", settings) is True

    def test_blacklist_mode(self):
        """Blacklist mode blocks listed extensions, allows others."""
        settings = {"upload_mode": "blacklist", "upload_blacklist": "zip, tar"}
        assert _is_extension_allowed_by_settings("zip", settings) is False
        assert _is_extension_allowed_by_settings("pdf", settings) is True
        assert _is_extension_allowed_by_settings("docx", settings) is True

    def test_allow_all_mode(self):
        """Allow-all mode accepts any extension except always-blocked."""
        settings = {"upload_mode": "allow_all"}
        assert _is_extension_allowed_by_settings("pdf", settings) is True
        assert _is_extension_allowed_by_settings("xyz", settings) is True
        assert _is_extension_allowed_by_settings("anything", settings) is True

    def test_none_settings_uses_default(self):
        """None settings falls back to allow-all defaults."""
        assert _is_extension_allowed_by_settings("pdf", None) is True
        assert _is_extension_allowed_by_settings("xyz", None) is True

    def test_platform_blocklist_overrides_upload_mode(self):
        """Host-controlled blocklist wins even when the wiki allows all files."""
        settings = {
            "upload_mode": "allow_all",
            "platform_upload_blacklist": "zip, mp4",
        }
        assert _is_extension_allowed_by_settings("zip", settings) is False
        assert _is_extension_allowed_by_settings("pdf", settings) is True


class TestAllowedAttachment:
    """Test the allowed_attachment function with settings."""

    def test_without_settings(self):
        """Without settings, uses allow-all defaults."""
        assert allowed_attachment("file.pdf") is True
        assert allowed_attachment("file.xyz") is True

    def test_with_allow_all_settings(self):
        """With allow_all settings, accepts new extensions."""
        settings = {"upload_mode": "allow_all"}
        assert allowed_attachment("file.pdf", settings=settings) is True
        assert allowed_attachment("file.xyz", settings=settings) is True
        assert allowed_attachment("file.exe", settings=settings) is False  # always blocked

    def test_with_blacklist_settings(self):
        """With blacklist settings, blocks listed extensions."""
        settings = {"upload_mode": "blacklist", "upload_blacklist": "pdf, zip"}
        assert allowed_attachment("file.pdf", settings=settings) is False
        assert allowed_attachment("file.docx", settings=settings) is True

    def test_no_extension(self):
        """Files without extension are rejected."""
        assert allowed_attachment("noext") is False
        assert allowed_attachment("noext", settings={"upload_mode": "allow_all"}) is False

    def test_platform_blocklist_blocks_attachments(self):
        settings = {
            "upload_mode": "allow_all",
            "platform_upload_blacklist": "zip",
        }
        assert allowed_attachment("file.zip", settings=settings) is False
        assert allowed_attachment("file.pdf", settings=settings) is True

    def test_platform_blocklist_blocks_chat_files(self):
        settings = {
            "upload_mode": "whitelist",
            "platform_upload_blacklist": "pdf",
        }
        assert allowed_chat_file("file.pdf", settings=settings) is False
        assert allowed_chat_file("file.txt", settings=settings) is True


class TestGetEffectiveMaxUploadSize:
    """Test upload size limit calculation."""

    def test_default_without_settings(self):
        """Without settings, returns 100 MB default."""
        assert get_effective_max_upload_size() == 100 * 1024 * 1024

    def test_custom_size(self):
        """Custom size from settings is used."""
        settings = {"upload_max_size_mb": 20}
        assert get_effective_max_upload_size(settings) == 20 * 1024 * 1024

    def test_zero_size_uses_default(self):
        """Zero size falls back to default, minimum is always at least 1 MB."""
        # 0 is treated as "not configured" and falls back to default (100 MB)
        settings = {"upload_max_size_mb": 0}
        assert get_effective_max_upload_size(settings) == 100 * 1024 * 1024
        # But max(1, ...) ensures at least 1 MB even with truthy low values
        settings2 = {"upload_max_size_mb": 1}
        assert get_effective_max_upload_size(settings2) == 1 * 1024 * 1024


class TestUploadSettingsSchema:
    """Test that upload settings columns exist in site_settings."""

    def test_upload_mode_default(self):
        """upload_mode defaults to 'allow_all'."""
        settings = db.get_site_settings()
        assert settings["upload_mode"] == "allow_all"

    def test_upload_blacklist_default(self):
        """upload_blacklist has a default set of dangerous extensions."""
        settings = db.get_site_settings()
        blacklist = settings["upload_blacklist"]
        assert "exe" in blacklist
        assert "bat" in blacklist

    def test_upload_max_size_mb_default(self):
        """upload_max_size_mb defaults to 100."""
        settings = db.get_site_settings()
        assert settings["upload_max_size_mb"] == 100

    def test_platform_upload_blacklist_default(self):
        """Host-controlled platform blocklist defaults to empty."""
        settings = db.get_site_settings()
        assert settings["platform_upload_blacklist"] == ""

    def test_update_upload_mode(self):
        """Upload mode can be changed via update_site_settings."""
        db.update_site_settings(upload_mode="allow_all")
        settings = db.get_site_settings()
        assert settings["upload_mode"] == "allow_all"

    def test_update_upload_whitelist(self):
        """Custom whitelist can be set."""
        db.update_site_settings(upload_whitelist="pdf, txt, md")
        settings = db.get_site_settings()
        assert settings["upload_whitelist"] == "pdf, txt, md"

    def test_update_upload_max_size(self):
        """Upload max size can be changed."""
        db.update_site_settings(upload_max_size_mb=50)
        settings = db.get_site_settings()
        assert settings["upload_max_size_mb"] == 50


class TestUploadSettingsRoute:
    """Test the admin settings route processes upload settings."""

    def test_settings_page_shows_upload_section(self, logged_in_admin):
        """The admin settings page shows the upload settings section."""
        resp = logged_in_admin.get("/global-settings")
        assert resp.status_code == 200
        assert b"Upload Settings" in resp.data
        assert b"upload_mode" in resp.data

    def test_save_upload_mode_allow_all(self, logged_in_admin):
        """Admin can save upload_mode=allow_all."""
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            "upload_mode": "allow_all",
            "upload_max_size_mb": "10",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["upload_mode"] == "allow_all"
        assert settings["upload_max_size_mb"] == 10

    def test_save_upload_mode_blacklist(self, logged_in_admin):
        """Admin can save upload_mode=blacklist with custom list."""
        resp = logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            "upload_mode": "blacklist",
            "upload_blacklist": "exe, bat, zip",
            "upload_max_size_mb": "20",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["upload_mode"] == "blacklist"
        assert "exe" in settings["upload_blacklist"]

    def test_invalid_upload_mode_defaults_to_allow_all(self, logged_in_admin):
        """Invalid upload mode is silently corrected to allow-all."""
        logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            "upload_mode": "invalid_mode",
            "upload_max_size_mb": "100",
        }, follow_redirects=True)
        settings = db.get_site_settings()
        assert settings["upload_mode"] == "allow_all"

    def test_upload_max_size_clamped(self, logged_in_admin):
        """Upload max size is clamped between 1 and 2048."""
        logged_in_admin.post("/global-settings", data={
            "site_name": "Test",
            "timezone": "UTC",
            "upload_mode": "whitelist",
            "upload_max_size_mb": "99999",
        }, follow_redirects=True)
        settings = db.get_site_settings()
        assert settings["upload_max_size_mb"] == 2048
