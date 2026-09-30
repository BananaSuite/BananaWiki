"""Page export: PDF, Markdown download, Markdown into the editor, and the bulk Markdown tools."""

import io
import os
import zipfile

from bananawiki.wiki.features.page_export import pdf
from bananawiki.wiki.features.pages import service

from .pages_support import in_app, make_category, make_page, restrict


def _pdf(client, slug):
    response = client.get(f"/page/{slug}/export-pdf")
    data = response.data
    response.close()
    return response.status_code, data


# ── PDF ───────────────────────────────────────────────────────────────────────


def test_pdf_export_for_permitted_users(app, client, make_user, login, db, admin):
    make_page(app, "Report", "# Hello\n\nSome **bold** text.")
    login(client, admin)
    status, data = _pdf(client, "report")
    assert status == 200 and data.startswith(b"%PDF")
    client.post("/logout")
    user = make_user("pdf_user")
    login(client, user)
    assert _pdf(client, "report")[0] == 403
    restrict(db, user, keys={"page.view_all", "page.export_pdf"})
    assert _pdf(client, "report")[0] == 200


def test_pdf_export_honours_site_setting(app, admin_client, db):
    make_page(app, "Off", "x")
    db.execute("UPDATE site_settings SET pdf_export_enabled = 0")
    assert _pdf(admin_client, "off")[0] == 404
    assert "export-pdf" not in admin_client.get("/page/off").get_data(as_text=True)


def test_pdf_of_invisible_page_is_404(app, client, make_user, login, db):
    secret = make_category(app, "Secret")
    make_page(app, "Classified", "x", category_id=secret["id"])
    user = make_user("pdf_restricted")
    restrict(db, user, keys={"page.view_all", "page.export_pdf"}, read=[])
    login(client, user)
    assert _pdf(client, "classified")[0] == 404


def test_pdf_handles_unicode_and_rich_markdown(app, admin_client):
    content = (
        "# Ωmega ✓ 中文 😀\n\nÈ più **grassetto _misto_** e `código`.\n\n"
        "| A | **B** |\n|---|---|\n| 1 <b>x</b> | [link](/page/x) |\n| only one |\n\n"
        "- one\n  - nested\n    1. deep\n- two\n\n> quote\n> > inner\n\n```python\ndef f():\n\treturn 1\n```\n\n"
        "---\n\n<script>alert(1)</script>\n\n[[video url=\"https://www.youtube.com/watch?v=abc\"]]\n"
    )
    make_page(app, "Ünïcode ✓", content)
    status, data = _pdf(admin_client, "ünïcode")
    assert status == 200 and data.startswith(b"%PDF")


def test_pdf_embeds_local_uploads_only(app, admin_client, monkeypatch):
    from PIL import Image

    folder = app.config["BW"].folders.uploads
    os.makedirs(folder, exist_ok=True)
    name = "a" * 32 + ".png"
    Image.new("RGB", (40, 30), "red").save(os.path.join(folder, name))

    def no_network(*args, **kwargs):
        raise AssertionError("the PDF export must not fetch anything")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    make_page(app, "Pictures", f"![local](/static/uploads/{name})\n\n![remote](https://example.com/x.png)\n\n"
                               "![evil](/static/uploads/../../secret.png)")
    status, data = _pdf(admin_client, "pictures")
    assert status == 200 and b"/Subtype /Image" in data


def test_simplifier_drops_remote_images_and_unsafe_links(app):
    def run():
        parser = pdf._Simplifier(base_url="https://wiki.example/", clean_text=lambda s: s, image_width=400)
        parser.feed('<p><img src="https://evil.example/x.png" alt="x"><a href="javascript:alert(1)">j</a>'
                    '<a href="/page/y">y</a></p><table><tr><td><b>b</b> c</td></tr></table>'
                    '<iframe src="https://x"></iframe>')
        return parser.result()

    with app.test_request_context():
        html = in_app(app, run)
    assert "evil.example" not in html and "javascript" not in html and "<iframe" not in html
    assert 'href="https://wiki.example/page/y"' in html
    assert "<td>b c</td>" in html


def test_pdf_falls_back_to_plain_text(app, admin_client, monkeypatch):
    def broken(*args, **kwargs):
        raise NotImplementedError("layout")

    monkeypatch.setattr(pdf, "_write_body", broken)
    make_page(app, "Fallback", "para one\n\npara two")
    status, data = _pdf(admin_client, "fallback")
    assert status == 200 and data.startswith(b"%PDF")


def test_history_revision_pdf(app, admin_client, db):
    page = make_page(app, "Versions", "v1")
    other = make_page(app, "Other", "x")
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=None, content="v2"))
    entry = db.scalar("SELECT MIN(id) FROM page_history WHERE page_id = ?", (page["id"],))
    response = admin_client.get(f"/page/versions/history/{entry}/export-pdf")
    assert response.status_code == 200 and response.data.startswith(b"%PDF")
    assert "revision" in response.headers["Content-Disposition"]
    response.close()
    assert admin_client.get(f"/page/{other['slug']}/history/{entry}/export-pdf").status_code == 404


# ── Markdown (single page) ────────────────────────────────────────────────────


def test_markdown_export_for_editors(app, client, make_user, login, db):
    cat = make_category(app, "Guides")
    make_page(app, "Café: \"quoted\"", "Body **text**", category_id=cat["id"])
    login(client, make_user("md_user"))
    assert client.get("/page/café-quoted/export-md").status_code == 403
    client.post("/logout")
    login(client, make_user("md_editor", role="editor"))
    response = client.get("/page/café-quoted/export-md")
    assert response.status_code == 200 and response.mimetype == "text/markdown"
    text = response.data.decode("utf-8")
    assert text.startswith('---\ntitle: "Café: \\"quoted\\""\nslug: "café-quoted"\ncategory: "Guides"\n')
    assert text.endswith("Body **text**")
    response.close()
    db.execute("UPDATE site_settings SET markdown_export_enabled = 0")
    assert client.get("/page/café-quoted/export-md").status_code == 404


def test_import_markdown_into_editor(app, client, make_user, login):
    make_page(app, "Target", "old")
    login(client, make_user("md_reader"))
    upload = {"import_file": (io.BytesIO(b"x"), "x.md")}
    assert client.post("/page/target/import-md", data=upload, content_type="multipart/form-data").status_code == 403
    client.post("/logout")
    login(client, make_user("md_importer", role="editor"))
    text = '---\ntitle: "New: title"\nslug: "ignored"\n---\n\n# Heading\n\nBody é'.encode()
    response = client.post("/page/target/import-md", data={"import_file": (io.BytesIO(text), "file.md")},
                           content_type="multipart/form-data")
    assert response.get_json() == {"content": "# Heading\n\nBody é", "title": "New: title"}
    latin = client.post("/page/target/import-md", data={"import_file": (io.BytesIO("caf\xe9".encode("latin-1")),
                                                                        "old.md")},
                        content_type="multipart/form-data")
    assert latin.get_json()["content"] == "café"
    assert in_app(app, lambda: service.get_by_slug("target"))["content"] == "old"


def test_export_menu_visibility(app, client, make_user, login, admin):
    make_page(app, "Menu", "x")
    login(client, make_user("menu_user"))
    assert "/page/menu/export-pdf" not in client.get("/page/menu").get_data(as_text=True)
    client.post("/logout")
    login(client, admin)
    html = client.get("/page/menu").get_data(as_text=True)
    assert "/page/menu/export-pdf" in html and "/page/menu/export-md" in html


# ── Bulk Markdown ─────────────────────────────────────────────────────────────


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    buffer.seek(0)
    return buffer


def _bulk_import(client, files, name="bundle.zip", **form):
    data = {"import_file": (_zip(files), name), **form}
    return client.post("/admin/bulk-markdown", data=data, content_type="multipart/form-data")


def _page(app, slug):
    return in_app(app, lambda: service.get_by_slug(slug))


def test_bulk_pages_are_admin_only(app, client, make_user, login):
    login(client, make_user("bulk_editor", role="editor"))
    assert client.get("/admin/bulk-markdown").status_code == 403
    assert client.post("/admin/bulk-markdown/export").status_code == 403
    assert _bulk_import(client, {"a.md": "# A"}).status_code == 403
    assert _page(app, "a") is None


def test_bulk_import_creates_categories_and_pages(app, admin_client, db):
    existing = make_category(app, "guides")
    response = _bulk_import(admin_client, {
        "01 - Guides/Basics/intro.md": "# Introduction\n\nWelcome",
        "Guides/second_page.md": "No heading here",
        "root.markdown": "---\ntitle: \"From front matter\"\n---\n# Other heading\n\ntext",
        "Guides/image.png": "binary",
        ".obsidian/workspace.md": "# hidden",
    })
    assert response.status_code == 302
    intro = _page(app, "introduction")
    assert intro["content"] == "Welcome" and intro["last_edited_by"] is None
    basics = db.one("SELECT * FROM categories WHERE id = ?", (intro["category_id"],))
    assert basics["name"] == "Basics" and basics["parent_id"] == existing["id"]
    assert _page(app, "second-page")["category_id"] == existing["id"]
    root = _page(app, "from-front-matter")
    assert root["category_id"] is None and root["content"].startswith("# Other heading")
    assert _page(app, "hidden") is None
    assert db.scalar("SELECT COUNT(*) FROM categories") == 2
    history = db.one("SELECT edited_by, edit_message FROM page_history WHERE page_id = ?", (intro["id"],))
    assert history["edited_by"] is None
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action = 'page_export.bulk_import'") == 1


def test_bulk_import_can_attribute_to_admin(app, admin_client, admin):
    _bulk_import(admin_client, {"mine.md": "# Mine"}, attribute_to_me="1")
    assert _page(app, "mine")["last_edited_by"] == admin["id"]


def test_bulk_import_modes(app, admin_client):
    make_page(app, "Existing", "old text")
    _bulk_import(admin_client, {"existing.md": "# Existing\n\nnew text"})
    assert _page(app, "existing")["content"] == "old text"
    _bulk_import(admin_client, {"existing.md": "# Existing\n\nnew text"}, mode="update")
    assert _page(app, "existing")["content"] == "new text"
    _bulk_import(admin_client, {"existing.md": "# Existing\n\ncopy"}, mode="copy")
    assert _page(app, "existing-2")["content"] == "copy"


def test_bulk_import_single_markdown_files(app, admin_client):
    data = {"import_file": [(io.BytesIO(b"# One"), "one.md"), (io.BytesIO(b"# Two"), "two.md"),
                            (io.BytesIO(b"x"), "evil.exe")]}
    response = admin_client.post("/admin/bulk-markdown", data=data, content_type="multipart/form-data",
                                 follow_redirects=True)
    assert _page(app, "one") and _page(app, "two")
    assert "evil.exe is not a Markdown file" in response.get_data(as_text=True)


def test_bulk_import_refuses_unsafe_archives(app, admin_client):
    response = _bulk_import(admin_client, {"../escape.md": "# Escape", "/abs.md": "# Abs", "ok.md": "# Ok"})
    assert response.status_code == 302
    assert _page(app, "escape") is None and _page(app, "abs") is None and _page(app, "ok") is not None
    bad = admin_client.post("/admin/bulk-markdown", data={"import_file": (io.BytesIO(b"not a zip"), "x.zip")},
                            content_type="multipart/form-data", follow_redirects=True)
    assert "not a valid ZIP archive" in bad.get_data(as_text=True)


def test_bulk_import_refuses_zip_bombs(app, admin_client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.md", "a" * (3 * 1024 * 1024))
    buffer.seek(0)
    response = admin_client.post("/admin/bulk-markdown", data={"import_file": (buffer, "bomb.zip")},
                                 content_type="multipart/form-data", follow_redirects=True)
    assert "ZIP bomb" in response.get_data(as_text=True)
    assert _page(app, "bomb") is None


def test_bulk_import_reports_oversized_pages(app, admin_client):
    _bulk_import(admin_client, {"big.md": "x" * 1_000_001, "fine.md": "# Fine"})
    assert _page(app, "fine") is not None and _page(app, "big") is None


def test_bulk_export_round_trip(app, admin_client, db):
    parent = make_category(app, "Docs")
    child = make_category(app, "How/To", parent_id=parent["id"])
    make_page(app, "Deep page", "Deep **content** ✓", category_id=child["id"])
    make_page(app, "Top page", "# Top page\n\nTop content")
    page = make_page(app, "With attachment", "![img](/static/uploads/" + "b" * 32 + ".png)")
    os.makedirs(app.config["BW"].folders.attachments, exist_ok=True)
    with open(os.path.join(app.config["BW"].folders.attachments, "stored.txt"), "wb") as fh:
        fh.write(b"attached")
    db.insert("page_attachments", {"page_id": page["id"], "filename": "stored.txt", "original_name": "notes.txt"})
    response = admin_client.post("/admin/bulk-markdown/export")
    assert response.status_code == 200 and response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    response.close()
    names = archive.namelist()
    assert "Docs/How_To/deep-page.md" in names and "top-page.md" in names and "manifest.json" in names
    assert "assets/page_attachments/with-attachment/notes.txt" in names
    deep = archive.read("Docs/How_To/deep-page.md").decode()
    assert 'category_path: "Docs/How∕To"' in deep and "# Deep page" in deep
    assert archive.read("top-page.md").decode().count("# Top page") == 2
    manifest = archive.read("manifest.json").decode()
    assert '"missing_assets"' in manifest and "uploads/" + "b" * 32 + ".png" in manifest

    files = {name: archive.read(name) for name in names if name.endswith(".md") and not name.startswith("assets")}
    before = db.scalar("SELECT COUNT(*) FROM page_history")
    _bulk_import(admin_client, files, mode="update")
    assert db.scalar("SELECT COUNT(*) FROM page_history") == before
    db.execute("DELETE FROM pages WHERE is_home = 0")
    db.execute("DELETE FROM categories")
    _bulk_import(admin_client, files)
    restored = _page(app, "deep-page")
    assert restored["content"] == "Deep **content** ✓"
    category = db.one("SELECT * FROM categories WHERE id = ?", (restored["category_id"],))
    assert category["name"] == "How/To"
    assert db.one("SELECT name FROM categories WHERE id = ?", (category["parent_id"],))["name"] == "Docs"
