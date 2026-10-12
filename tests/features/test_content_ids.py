"""Content cannot stand in for the interface's own elements.

Content keeps the ids of its headings: the table of contents gives each heading
one from its text ("## Kanban share dialog" becomes ``id="kanban-share-dialog"``)
and a raw ``<h2 id="…">`` keeps its own. On many pages the content comes before
the interface's dialogs, forms and panels, and whatever looks an element up by
its id alone (``document.getElementById``, ``label for=``, ``aria-labelledby``,
a link to ``#id``) gets the first element with that id. So:

* scripts find their elements by tag (``dialog#…``: content cannot make a
  dialog), by a data attribute, inside their own container, or once, before
  any content is drawn into it;
* labels that a heading could take over hold their control instead of naming it;
* headings never take an id the interface links to (``markdown.RESERVED_IDS``),
  and the announcement bar, above every page, keeps no heading ids.

Each page here is rendered with headings named after the interface's ids, to
show that they do come first; the markup and the scripts are then checked.
tests/features/test_content_ids_script.py runs the scripts on such pages.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from bananawiki.wiki import markdown
from bananawiki.wiki.db import connection_scope

from .pages_support import make_page
from .test_kanban import as_client, board_id_from, columns, new_ticket, set_settings

WIKI = Path(__file__).parents[2] / "bananawiki" / "wiki"
SCRIPTS = sorted(path for path in WIKI.rglob("*.js") if "vendor" not in path.parts)
TEMPLATES = sorted(WIKI.rglob("*.html"))

# A board description and a ticket comment named after the board's own elements.
BOARD_TRAP = ("## Kanban board\n\n## Kanban ticket dialog\n\n## Kanban filter\n\n## Kanban share dialog\n\n"
              "## Kanban edit dialog\n\n## Kanban delete dialog\n\n## Kanban offline\n\n## Kanban archive\n\n"
              "## Kanban activity\n\n## Kanban bulk\n\n## Kanban lanes\n\nHow we work.")
COMMENT_TRAP = ("## Kanban ticket assignees\n\n## Kanban ticket priority\n\n## Kanban comment input\n\n"
                "## Kanban upload input\n\nLooks good.")
PAGE_TRAP = ("## Main\n\n## Sidebar\n\n## Attachments\n\n## Dlg title\n\n## Move category\n\n## Dlg slug\n\n"
             '<h2 id="BWPagesNav">Navigation</h2>\n\n<h2 id="BW">Core</h2>\n\nText.')


def _source(relative: str) -> str:
    return (WIKI / relative).read_text(encoding="utf-8")


def _at(html: str, needle: str) -> int:
    index = html.find(needle)
    assert index >= 0, needle
    return index


# ── Scripts ───────────────────────────────────────────────────────────────────

BARE_ID_LOOKUP = re.compile(r"getElementById\(|document\.querySelector(?:All)?\(\s*[\"']#")
TAGGED_LOOKUP = re.compile(r"""querySelector(?:All)?\(\s*["']([a-z]+)#([\w-]+)["']\s*\)""")


def test_scripts_never_look_an_element_up_by_its_id_alone():
    found = [f"{path.relative_to(WIKI)}:{number}" for path in SCRIPTS
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if BARE_ID_LOOKUP.search(line)]
    assert found == []


def test_tagged_lookups_name_the_tag_the_template_gives_the_element():
    templates = "\n".join(path.read_text(encoding="utf-8") for path in TEMPLATES)
    checked = 0
    for script in SCRIPTS:
        for tag, element_id in TAGGED_LOOKUP.findall(script.read_text(encoding="utf-8")):
            tags = set(re.findall(rf'<([a-z]+)\b[^>]*\bid="{re.escape(element_id)}"', templates))
            assert tags == {tag}, (script.name, f"{tag}#{element_id}", tags)
            checked += 1
    assert checked >= 40


def test_run_once_marks_and_the_core_object_are_not_read_from_window_names():
    # An element with an id is a property of window (named access) until a script sets that name.
    for path in SCRIPTS:
        source = path.read_text(encoding="utf-8")
        assert "window.BW || " not in source and "window.BWPagesNav" not in source, path.name
    for relative in ("static/js/bananawiki.js", "features/canvas/static/canvas-core.js"):
        assert 'Object.prototype.hasOwnProperty.call(window, "BW") ? window.BW : {}' in _source(relative)
    navigation = _source("features/pages/static/navigation.js")
    assert "if (BW.pagesNav) return;\n" in navigation.replace("\r\n", "\n")


# ── Markdown ──────────────────────────────────────────────────────────────────


def test_headings_never_take_an_id_the_interface_links_to():
    html = markdown.render("[TOC]\n\n## Main\n\n## Main\n\n## Sidebar\n\n## Attachments\n\n## Mascot config\n\n"
                           '<h2 id="sidebar">Raw</h2>\n\n<h2 id="Main">Case</h2>\n\n<h2 id="BWPagesNav">Nav</h2>')
    assert re.findall(r'<h2 id="([^"]+)"', html) == [
        "main_1", "main_2", "sidebar_1", "attachments_1", "mascot-config", "sidebar_1", "Main", "BWPagesNav"]
    assert 'href="#main_1"' in html and 'href="#attachments_1"' in html
    # The reserved ids are the interface's: the shell's skip-link target and sidebar, the attachments panel.
    assert {"main", "sidebar", "attachments"} == markdown.RESERVED_IDS
    base = (WIKI / "templates/base.html").read_text(encoding="utf-8")
    assert '<main id="main"' in base and 'id="sidebar"' in base and 'href="#main"' in base
    assert 'id="attachments"' in _source("features/attachments/templates/attachments/_page_panel.html")
    assert '"#attachments"' in _source("features/attachments/routes.py")


def test_builder_headings_never_take_an_id_the_interface_links_to(app):
    from bananawiki.wiki.features.page_builder import document

    doc = document.validate({"version": 2, "blocks": [
        {"type": "heading", "text": "Main"}, {"type": "heading", "text": "Sidebar"},
        {"type": "heading", "text": "Main"}, {"type": "text", "format": "markdown", "text": "## Attachments"}]})
    with app.test_request_context():
        html = str(document.render(doc))
    assert re.findall(r'<h\d id="([^"]+)"', html) == ["main-2", "sidebar-2", "main-3", "attachments_1"]


# ── Kanban ────────────────────────────────────────────────────────────────────


def _trapped_board(app, admin_client, db, make_user, login, description=BOARD_TRAP):
    """A private board: a writer (write share) wrote the description, a viewer (view share) a comment."""
    set_settings(db, kanban_access="all")
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap", "description": "Plans"}))
    admin_client.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "private"})
    writer_user, viewer_user = make_user("board_writer"), make_user("board_viewer")
    for user, level in ((writer_user, "write"), (viewer_user, "view")):
        admin_client.post(f"/kanban/{board}/share", data={"action": "add_user", "user_id": user["id"],
                                                          "access_level": level})
    writer, viewer = as_client(app, login, writer_user), as_client(app, login, viewer_user)
    assert writer.post(f"/kanban/{board}/edit", data={"title": "Roadmap", "description": description}).status_code == 302
    # Sharing stays the owner's, and its dialog is the only page for it.
    assert writer.post(f"/kanban/{board}/share", data={"action": "set_visibility", "visibility": "public"}
                       ).status_code == 403
    admin_id = db.scalar("SELECT id FROM users WHERE username = 'admin_user'")
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"], "Spec",
                        assignees=[admin_id, writer_user["id"]])
    assert viewer.put(f"/api/kanban/tickets/{ticket['id']}", json={"assignees": []}).status_code == 403
    comment = viewer.post(f"/api/kanban/tickets/{ticket['id']}/comments", json={"content": COMMENT_TRAP})
    assert comment.status_code == 201
    return board, ticket, writer_user


def test_board_description_and_comments_come_before_the_board_and_its_dialogs(app, admin_client, db,
                                                                               make_user, login):
    board, ticket, _ = _trapped_board(app, admin_client, db, make_user, login)
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    # The writer's headings come first: every lookup by id alone found them.
    description = _at(html, 'id="kanban-board-description"')
    for heading, element in (("kanban-board", '<div class="kanban-board" id="kanban-board"'),
                             ("kanban-ticket-dialog", '<dialog id="kanban-ticket-dialog"'),
                             ("kanban-filter", 'id="kanban-filter" role="search"'),
                             ("kanban-share-dialog", '<dialog id="kanban-share-dialog"'),
                             ("kanban-edit-dialog", '<dialog id="kanban-edit-dialog"'),
                             ("kanban-delete-dialog", '<dialog id="kanban-delete-dialog"'),
                             ("kanban-offline", 'id="kanban-offline" hidden')):
        assert description < _at(html, f'<h2 id="{heading}">') < _at(html, element), heading
    # The viewer's comment carries ids too; the dialog draws comments above its side panel.
    comments = admin_client.get(f"/api/kanban/tickets/{ticket['id']}/comments").get_json()
    assert 'id="kanban-ticket-assignees"' in comments[0]["content_html"]
    assert _at(html, 'id="kanban-ticket-comments"') < _at(html, '<div id="kanban-ticket-assignees">')
    assert _at(html, 'id="kanban-ticket-comments"') < _at(html, 'id="kanban-comment-input"')


def test_board_scripts_find_their_elements_by_tag_or_once(app, admin_client, db, make_user, login):
    board, _, _ = _trapped_board(app, admin_client, db, make_user, login)
    html = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    ticket = _source("features/kanban/static/kanban-ticket.js")
    assert 'var dialog = document.querySelector("dialog#kanban-ticket-dialog");' in ticket
    assert 'dialog.querySelectorAll("[id]")' in ticket and "function $(id) { return controls[id] || null; }" in ticket
    board_js = _source("features/kanban/static/kanban-board.js")
    for lookup in ('document.querySelector("div#kanban-board")', 'document.querySelector("div#kanban-offline")',
                   'document.querySelector("[data-kanban-title]")', 'document.querySelector("section#kanban-activity")'):
        assert lookup in board_js, lookup
    assert '<h1 id="kanban-board-title" data-kanban-title>' in html
    assert 'document.querySelector("form#kanban-filter")' in _source("features/kanban/static/kanban-filter.js")
    bulk = _source("features/kanban/static/kanban-bulk.js")
    assert 'document.querySelector("section#kanban-bulk")' in bulk and 'panel.querySelector("#" + id)' in bulk
    assert 'document.querySelector("section#kanban-archive")' in _source("features/kanban/static/kanban-archive.js")
    assert 'document.querySelector("select#kanban-lanes")' in _source("features/kanban/static/kanban-lanes.js")
    # The owner's Edit, Sharing and Delete open through the shared opener, by tag.
    assert 'document.querySelector("dialog#" + CSS.escape(dialogId))' in _source("static/js/bananawiki.js")
    # A comment heading cannot take the Upload button's file input: the label holds it.
    assert 'for="kanban-upload-input"' not in html
    assert re.search(r'<label class="btn btn--small">\s*<span>[^<]+</span>\s*<input type="file" '
                     r'id="kanban-upload-input" class="visually-hidden">\s*</label>', html)


# ── Pages ─────────────────────────────────────────────────────────────────────


def _trapped_page(client, make_user, login):
    login(client, make_user("page_trapper", role="editor"))
    created = client.post("/create-page", data={"title": "Id trap", "content": PAGE_TRAP})
    assert created.status_code == 302
    client.post("/logout")
    return created.headers["Location"]


def test_page_headings_keep_away_from_the_layout_and_the_page_dialogs(client, make_user, login):
    url = _trapped_page(client, make_user, login)
    login(client, make_user("page_admin", role="admin"))
    html = client.get(url).get_data(as_text=True)
    # Headings named after the layout's ids get "_1", and the page's contents list leads to them.
    for name in ("main", "sidebar", "attachments"):
        assert f'<h2 id="{name}_1">' in html and f'href="#{name}_1"' in html
        assert f'<h2 id="{name}">' not in html
    assert html.count('id="main"') == 1 and '<main id="main"' in html
    assert html.count('id="sidebar"') == 1 and 'class="sidebar" id="sidebar"' in html
    # Other headings keep their ids, and they come before the page's dialogs...
    for name, element in (("dlg-title", '<dialog id="dlg-title"'), ("dlg-slug", '<dialog id="dlg-slug"'),
                          ("move-category", '<select id="move-category"')):
        assert _at(html, f'<h2 id="{name}">') < _at(html, element), name
    # ...so the dialogs open by tag, and the Move dialog's label holds its list.
    assert 'document.querySelector("dialog#" + CSS.escape(dialogId))' in _source("static/js/bananawiki.js")
    assert 'for="move-category"' not in html
    assert re.search(r'<label class="field">\s*<span class="label">[^<]+</span>\s*<select id="move-category"', html)
    # The contents card is named directly, not by an id a heading could take.
    assert 'aria-labelledby="toc-title"' not in html and '<nav class="toc-card" aria-label="' in html
    # Raw headings keep ids that match names scripts once read from window.
    assert '<h2 id="BWPagesNav">' in html and '<h2 id="BW">' in html
    assert re.search(r'<script src="[^"]*/navigation\.js[^"]*" defer>', html)


def test_attachments_return_to_their_panel_not_to_a_heading(app, admin_client):
    page = make_page(app, "Handbook", "## Attachments\n\nWhat to attach.\n\n## Notes")
    response = admin_client.post(f"/page/{page['slug']}/attachments", content_type="multipart/form-data",
                                 data={"files": (io.BytesIO(b"hello"), "notes.txt")})
    assert response.status_code == 302 and response.headers["Location"].endswith("#attachments")
    html = admin_client.get(f"/page/{page['slug']}").get_data(as_text=True)
    assert html.count('id="attachments"') == 1
    assert _at(html, '<h2 id="attachments_1">') < _at(html, 'class="card attachments no-print" id="attachments"')


def test_editor_preview_cannot_take_the_editor_fields_and_dialogs(app, client, make_user, login):
    login(client, make_user("editor_trapper", role="editor"))
    page = make_page(app, "Draft", "## Edit message\n\n## Dlg link\n\n## Attachments editor input\n\n"
                                   "## Md import input")
    # The preview shows the page with its headings' ids, above the dialogs and the fields under the editor.
    preview = client.post("/api/preview", json={"content": "## Edit message\n\n## Dlg link"}).get_json()["html"]
    assert '<h2 id="edit-message">' in preview and '<h2 id="dlg-link">' in preview
    for url in (f"/page/{page['slug']}/edit", "/create-page"):
        html = client.get(url).get_data(as_text=True)
        assert _at(html, 'id="editor-preview"') < _at(html, '<dialog id="dlg-link"') < _at(html, 'id="edit-message"')
        assert 'for="edit-message"' not in html
        assert re.search(r'<label class="field"><span class="label">[^<]+</span>\s*<input id="edit-message"', html)
    html = client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    for input_id in ("attachments-editor-input", "md-import-input"):
        assert f'for="{input_id}"' not in html, input_id
        assert re.search(rf'<label class="btn btn--small">\s*<span>[^<]+</span>\s*<input id="{input_id}"', html), input_id
    editor = _source("features/pages/static/editor.js")
    assert 'var dialog = document.querySelector("dialog#" + CSS.escape(id));' in editor
    assert 'var linkDialog = document.querySelector("dialog#dlg-link");' in editor


# ── Canvas ────────────────────────────────────────────────────────────────────


def test_canvas_text_cannot_take_the_editor_dialogs(app, client, make_user, login):
    from bananawiki.wiki.features.canvas import service

    owner = make_user("canvas_trapper", role="editor")
    with app.test_request_context(), connection_scope():
        canvas = service.create("Board", "", owner["id"])
    login(client, owner)
    node = {"id": "n1", "type": "text", "x": 10, "y": 20,
            "content": "## Canvas node dialog\n\n## Canvas edge dialog\n\n## Cn page results"}
    response = client.post(f"/canvas/{canvas['slug']}/ops", json={"ops": [{"op": "upsert_node", "node": node}]},
                           headers={"X-Canvas-Session": "s1"})
    rendered = response.get_json()["rendered"]["n1"]["html"]
    for name in ("canvas-node-dialog", "canvas-edge-dialog", "cn-page-results"):
        assert f'<h2 id="{name}">' in rendered
    # The stage, where that text is drawn, comes before the dialogs.
    html = client.get(f"/canvas/{canvas['slug']}").get_data(as_text=True)
    assert _at(html, "data-canvas-stage") < _at(html, '<dialog id="canvas-node-dialog"') < _at(
        html, '<dialog id="canvas-edge-dialog"')
    editor = _source("features/canvas/static/canvas-editor.js")
    assert 'var nodeDialog = this.nodeDialog = document.querySelector("dialog#canvas-node-dialog");' in editor
    assert 'var edgeDialog = this.edgeDialog = document.querySelector("dialog#canvas-edge-dialog");' in editor
    assert editor.count("var dialog = this.nodeDialog;") == 1 and editor.count("var dialog = this.edgeDialog;") == 1
    assert 'var results = this.nodeDialog.querySelector("#cn-page-results");' in editor
    # The history preview reads its snapshot from a script element.
    assert 'document.querySelector("script#" + CSS.escape(sourceId))' in _source("features/canvas/static/canvas-static.js")


# ── Announcements ─────────────────────────────────────────────────────────────


def test_announcement_headings_keep_no_ids(admin_client, db):
    response = admin_client.post("/admin/announcements/create", data={
        "content": "## Kanban board\n\n## Page builder\n\n## Main\n\nTonight.", "color": "blue",
        "text_size": "normal", "visibility": "both", "audience_mode": "all", "not_removable": "1"})
    assert response.status_code == 302
    html = admin_client.get("/kanban").get_data(as_text=True)
    bar = html[_at(html, 'id="announcements-bar"'):_at(html, '<header class="topbar"')]
    assert "<h2>Kanban board</h2>" in bar and "<h2>Page builder</h2>" in bar and "<h2>Main</h2>" in bar
    assert not re.search(r"<h[1-6] id=", bar)
    # The skip link reaches the page's main region.
    assert html.count('id="main"') == 1 and '<main id="main"' in html
