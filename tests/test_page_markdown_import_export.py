"""Tests for the page edit-mode Markdown import/export feature."""

import io

import db
from helpers._text import slugify


def _create_page(user_id, title="Test Page", content="# Hello\n\nWorld"):
    slug = slugify(title)
    db.create_page(title, slug, content, user_id=user_id)
    return slug


class TestPageMarkdownExport:
    def test_editor_can_export_markdown(self, client, editor_user, admin_user):
        # Admin creates a page so the editor can export it.
        slug = _create_page(admin_user, "Recipe", "# Recipe\n\nPasta with sauce.")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.get(f"/page/{slug}/export-md")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["Content-Type"]
        body = resp.data.decode("utf-8")
        assert body.startswith("---\n")
        assert 'title: "Recipe"' in body
        assert f'slug: "{slug}"' in body
        assert "# Recipe\n\nPasta with sauce." in body
        # The download filename should be <slug>.md
        assert 'filename="recipe.md"' in resp.headers["Content-Disposition"]

    def test_regular_user_cannot_export_markdown(self, client, regular_user, admin_user):
        slug = _create_page(admin_user, "Secret", "# Top secret")
        client.post("/login", data={"username": "user", "password": "user123"})
        resp = client.get(f"/page/{slug}/export-md", follow_redirects=False)
        # @editor_required redirects non-editors back home.
        assert resp.status_code in (302, 403)
        if resp.status_code == 302:
            # Should not be redirected to the export URL itself.
            assert "/export-md" not in resp.headers.get("Location", "")

    def test_anonymous_cannot_export_markdown(self, client, admin_user):
        slug = _create_page(admin_user, "Public Notes", "Hello")
        resp = client.get(f"/page/{slug}/export-md", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_export_unknown_page_returns_404(self, client, editor_user, admin_user):
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.get("/page/does-not-exist/export-md")
        assert resp.status_code == 404

    def test_export_handles_quotes_in_title(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, 'A "Quoted" Title', "body")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.get(f"/page/{slug}/export-md")
        assert resp.status_code == 200
        # Embedded quotes must be backslash-escaped so the front-matter
        # round-trips cleanly when re-imported.
        body = resp.data.decode("utf-8")
        assert 'title: "A \\"Quoted\\" Title"' in body


class TestPageMarkdownImport:
    def _post_import(self, client, slug, filename, content):
        return client.post(
            f"/page/{slug}/import-md",
            data={"import_file": (io.BytesIO(content.encode("utf-8")), filename)},
            content_type="multipart/form-data",
        )

    def test_editor_can_import_plain_markdown(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = self._post_import(client, slug, "import.md", "# Imported\n\nHello.")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["content"] == "# Imported\n\nHello."
        # No front-matter, no title returned.
        assert body["title"] is None

    def test_import_extracts_frontmatter_title(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        md = (
            "---\n"
            'title: "New Heading"\n'
            'slug: "ignored"\n'
            "---\n\n"
            "# Body\n"
        )
        resp = self._post_import(client, slug, "import.md", md)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["title"] == "New Heading"
        assert body["content"] == "# Body\n"

    def test_import_normalises_crlf(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = self._post_import(client, slug, "import.md", "Line A\r\nLine B")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["content"] == "Line A\nLine B"

    def test_regular_user_cannot_import(self, client, regular_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "user", "password": "user123"})
        resp = self._post_import(client, slug, "import.md", "hi")
        # @editor_required short-circuits before we ever reach the
        # handler, so we get a redirect rather than 403 JSON.
        assert resp.status_code in (302, 403)

    def test_anonymous_cannot_import(self, client, admin_user):
        slug = _create_page(admin_user, "Target")
        resp = self._post_import(client, slug, "import.md", "hi")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_import_missing_file_returns_400(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = client.post(f"/page/{slug}/import-md")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "no_file"

    def test_import_oversized_file_rejected(self, client, editor_user, admin_user):
        slug = _create_page(admin_user, "Target")
        client.post("/login", data={"username": "editor", "password": "editor123"})
        # 1 MB + 1 byte trips the cap.
        oversize = "a" * (1_000_001)
        resp = self._post_import(client, slug, "huge.md", oversize)
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "too_large"

    def test_import_unknown_page_returns_404(self, client, editor_user, admin_user):
        client.post("/login", data={"username": "editor", "password": "editor123"})
        resp = self._post_import(client, "does-not-exist", "import.md", "hi")
        assert resp.status_code == 404
