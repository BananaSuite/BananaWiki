"""Tests for the PDF export feature."""

import io

import pytest
from werkzeug.security import generate_password_hash

import db


def _enable_pdf_export():
    """Enable PDF export in site settings."""
    db.update_site_settings(pdf_export_enabled=1)


def _grant_pdf_permission(user_id):
    """Grant the page.export_pdf permission to a specific user."""
    perms = db.get_user_permissions(user_id)
    enabled = set(perms["enabled_permissions"])
    enabled.add("page.export_pdf")
    db.set_user_permissions(user_id, list(enabled))


def _create_page(user_id, title="Test Page", content="# Hello\n\nWorld"):
    """Create a wiki page and return its slug."""
    from helpers._text import slugify
    slug = slugify(title)
    db.create_page(title, slug, content, user_id=user_id)
    return slug


class TestPDFGeneration:
    """Tests for the generate_page_pdf helper."""

    def test_generate_pdf_returns_bytes(self, isolated_db):
        from helpers._pdf import generate_page_pdf
        buf = generate_page_pdf("Test", "# Hello\nWorld", "MySite")
        assert isinstance(buf, io.BytesIO)
        data = buf.read()
        assert len(data) > 0
        assert data[:5] == b"%PDF-"

    def test_generate_pdf_with_metadata(self, isolated_db):
        from helpers._pdf import generate_page_pdf
        buf = generate_page_pdf(
            "My Page", "Some **bold** content", "Wiki",
            author="alice", edited_at="2025-01-01 12:00", is_history=True,
        )
        data = buf.read()
        assert data[:5] == b"%PDF-"

    def test_strip_markdown_basics(self, isolated_db):
        from helpers._pdf import _strip_markdown
        result = _strip_markdown("# Heading\n\n**bold** and *italic*\n\n[link](http://x)")
        assert "Heading" in result
        assert "bold" in result
        assert "italic" in result
        assert "link" in result
        assert "**" not in result
        assert "*italic*" not in result

    def test_generate_pdf_handles_rich_markdown(self, isolated_db):
        """The styled renderer should accept the full Markdown subset without
        raising and still produce a valid PDF.
        """
        from helpers._pdf import generate_page_pdf
        rich = (
            "# Title\n\n"
            "Paragraph with **bold**, *italic*, `code` and [a link](https://x).\n\n"
            "## Section\n\n"
            "- item 1\n- item 2 with **bold**\n- item 3\n\n"
            "1. first\n2. second\n\n"
            "```python\nprint('hello')\n```\n\n"
            "> a quote across\n> two lines\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "---\n\nFooter paragraph.\n"
        )
        buf = generate_page_pdf("Rich", rich, "Wiki")
        data = buf.read()
        assert data[:5] == b"%PDF-"
        assert len(data) > 1000  # styled rendering produces a non-trivial file

    def test_generate_pdf_handles_empty_content(self, isolated_db):
        """An empty page should still produce a valid PDF cover page."""
        from helpers._pdf import generate_page_pdf
        buf = generate_page_pdf("Empty", "", "Wiki")
        data = buf.read()
        assert data[:5] == b"%PDF-"

    def test_parse_inline_basic_formatting(self, isolated_db):
        from helpers._pdf import _parse_inline
        runs = _parse_inline("Hello **bold** and *italic* and `code` here.")
        joined = "|".join(
            f"{r['text']}({'B' if r['bold'] else ''}{'I' if r['italic'] else ''}"
            f"{'C' if r['code'] else ''})"
            for r in runs
        )
        assert "bold(B)" in joined
        assert "italic(I)" in joined
        assert "code(C)" in joined

    def test_parse_inline_link_carries_url(self, isolated_db):
        from helpers._pdf import _parse_inline
        runs = _parse_inline("see [the docs](https://example.com/doc) please")
        link_runs = [r for r in runs if r["link"]]
        assert link_runs and link_runs[0]["link"] == "https://example.com/doc"
        assert any(r["text"] == "the docs" for r in link_runs)

    def test_parse_blocks_recognises_block_types(self, isolated_db):
        from helpers._pdf import _parse_blocks
        text = (
            "# H1\n\nP\n\n"
            "```\ncode\n```\n\n"
            "> quote\n\n"
            "- item\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "---\n"
        )
        kinds = [b["type"] for b in _parse_blocks(text)]
        for expected in ("heading", "paragraph", "code", "quote",
                         "list", "table", "hr"):
            assert expected in kinds, f"missing {expected} in {kinds}"


class TestPDFExportDisabledByDefault:
    """When pdf_export_enabled=0 (default), export links should not appear."""

    def test_setting_defaults_to_enabled(self, isolated_db):
        settings = db.get_site_settings()
        assert settings["pdf_export_enabled"] == 1

    def test_permission_defaults_to_false_for_editor(self, isolated_db):
        uid = db.create_user("ed", generate_password_hash("ed123"), role="editor")
        assert not db.has_permission({"id": uid, "role": "editor"}, "page.export_pdf")

    def test_permission_defaults_to_false_for_user(self, isolated_db):
        uid = db.create_user("usr", generate_password_hash("usr123"), role="user")
        assert not db.has_permission({"id": uid, "role": "user"}, "page.export_pdf")

    def test_admin_always_has_permission(self, isolated_db):
        uid = db.create_user("adm", generate_password_hash("adm123"), role="admin")
        assert db.has_permission({"id": uid, "role": "admin"}, "page.export_pdf")

    def test_export_route_blocked_when_disabled(self, client, admin_user):
        """Even admins can't export if the feature is disabled."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=0)
        slug = _create_page(admin_user, "ExportTest", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        # Should redirect with error (feature disabled)
        assert resp.status_code == 302

    def test_page_template_no_export_link_when_disabled(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=0)
        slug = _create_page(admin_user, "NoExport", "Content")
        resp = client.get(f"/page/{slug}")
        assert b"/export-pdf" not in resp.data


class TestPDFExportEnabled:
    """When pdf_export_enabled=1 and permission granted, export should work."""

    def test_admin_can_export_page(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        slug = _create_page(admin_user, "PDF Page", "# Title\n\nSome content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"
        assert resp.data[:5] == b"%PDF-"

    def test_editor_with_permission_can_export(self, client, admin_user, editor_user):
        _enable_pdf_export()
        _grant_pdf_permission(editor_user)
        client.post("/login", data={"username": "editor", "password": "editor123"})
        slug = _create_page(admin_user, "Editor PDF", "Content here")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"

    def test_editor_without_permission_denied(self, client, admin_user, editor_user):
        _enable_pdf_export()
        # Do NOT grant permission
        client.post("/login", data={"username": "editor", "password": "editor123"})
        slug = _create_page(admin_user, "Blocked PDF", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 302  # redirect with error

    def test_regular_user_with_permission_can_export(self, client, admin_user, regular_user):
        _enable_pdf_export()
        _grant_pdf_permission(regular_user)
        client.post("/login", data={"username": "user", "password": "user123"})
        slug = _create_page(admin_user, "User PDF", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"

    def test_regular_user_without_permission_denied(self, client, admin_user, regular_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "user", "password": "user123"})
        slug = _create_page(admin_user, "Denied PDF", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 302

    def test_nonexistent_page_404(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/page/nonexistent-slug/export-pdf")
        assert resp.status_code == 404

    def test_unauthenticated_user_redirected(self, client, admin_user):
        _enable_pdf_export()
        slug = _create_page(admin_user, "Auth PDF", "Content")
        resp = client.get(f"/page/{slug}/export-pdf")
        assert resp.status_code == 302

    def test_page_template_shows_export_link(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        slug = _create_page(admin_user, "Show Link", "Content")
        resp = client.get(f"/page/{slug}")
        assert b"Export as PDF" in resp.data

    def test_page_template_hides_export_link_without_permission(self, client, admin_user, editor_user):
        _enable_pdf_export()
        # Editor without page.export_pdf permission should NOT see the link
        client.post("/login", data={"username": "editor", "password": "editor123"})
        slug = _create_page(admin_user, "Hidden Link", "Content")
        resp = client.get(f"/page/{slug}")
        assert b"/export-pdf" not in resp.data


class TestHistoryPDFExport:
    """Tests for exporting page history entries as PDF."""

    def test_admin_can_export_history_entry(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        slug = _create_page(admin_user, "History Page", "Version 1")
        page = db.get_page_by_slug(slug)
        # Edit page to create a history entry
        db.update_page(page["id"], "History Page", "Version 2", admin_user, "edit")
        history = list(db.get_page_history(page["id"]))
        assert len(history) > 0
        entry_id = history[0]["id"]
        resp = client.get(f"/page/{slug}/history/{entry_id}/export-pdf")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"

    def test_history_export_blocked_when_disabled(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        db.update_site_settings(pdf_export_enabled=0)
        slug = _create_page(admin_user, "Hist Blocked", "V1")
        page = db.get_page_by_slug(slug)
        db.update_page(page["id"], "Hist Blocked", "V2", admin_user, "edit")
        history = list(db.get_page_history(page["id"]))
        entry_id = history[0]["id"]
        resp = client.get(f"/page/{slug}/history/{entry_id}/export-pdf")
        assert resp.status_code == 302

    def test_history_export_nonexistent_entry_404(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        slug = _create_page(admin_user, "No History", "Content")
        resp = client.get(f"/page/{slug}/history/99999/export-pdf")
        assert resp.status_code == 404


class TestAdminPDFSettings:
    """Tests for the PDF export admin settings toggle."""

    def test_admin_can_enable_pdf_export(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings", data={
            "site_name": "Test Wiki",
            "timezone": "UTC",
            "pdf_export_enabled": "1",
            "default_theme_mode": "dark",
            "bg_color": "#16161f",
            "sidebar_color": "#1a1a24",
            "secondary_color": "#1e1e2c",
            "text_color": "#c8ccd8",
            "primary_color": "#8fa0d4",
            "accent_color": "#7e9ada",
            "light_bg_color": "#f6f7fb",
            "light_sidebar_color": "#e9edf5",
            "light_secondary_color": "#ffffff",
            "light_text_color": "#202534",
            "light_primary_color": "#4b63b6",
            "light_accent_color": "#3553c7",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["pdf_export_enabled"] == 1

    def test_admin_can_disable_pdf_export(self, client, admin_user):
        _enable_pdf_export()
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.post("/global-settings", data={
            "site_name": "Test Wiki",
            "timezone": "UTC",
            # pdf_export_enabled intentionally omitted → checkbox unchecked
            "default_theme_mode": "dark",
            "bg_color": "#16161f",
            "sidebar_color": "#1a1a24",
            "secondary_color": "#1e1e2c",
            "text_color": "#c8ccd8",
            "primary_color": "#8fa0d4",
            "accent_color": "#7e9ada",
            "light_bg_color": "#f6f7fb",
            "light_sidebar_color": "#e9edf5",
            "light_secondary_color": "#ffffff",
            "light_text_color": "#202534",
            "light_primary_color": "#4b63b6",
            "light_accent_color": "#3553c7",
        }, follow_redirects=True)
        assert resp.status_code == 200
        settings = db.get_site_settings()
        assert settings["pdf_export_enabled"] == 0

    def test_settings_page_shows_pdf_toggle(self, client, admin_user):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        resp = client.get("/global-settings")
        assert resp.status_code == 200
        assert b"PDF Export" in resp.data
        assert b"pdf_export_enabled" in resp.data
