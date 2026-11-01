"""Regression tests for the o2 findings of the full re-audit pass 9 (canvas: page-link updates, history pages)."""

from __future__ import annotations

import json

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.canvas import model, present, service
from bananawiki.wiki.features.pages import service as pages
from tests.features.pages_support import make_page


def run(app, fn):
    with app.test_request_context(), connection_scope():
        return fn()


def _document(*nodes):
    return {"nodes": list(nodes), "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}


def _store(db, layout, doc):
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (json.dumps(doc), layout["id"]))


def _parsed_layouts(monkeypatch):
    """Ids of the canvases whose stored document the page-link rewrite parses."""
    parsed: list[str] = []
    clean = model.clean_document

    def counting(raw, **kwargs):
        if isinstance(raw, str):
            parsed.append(raw)
        return clean(raw, **kwargs)

    monkeypatch.setattr(service.model, "clean_document", counting)
    return parsed


# OW2-1: a page event parses only the canvases whose wiki-page nodes store that page's slug


def test_ow2_1_page_title_change_skips_canvases_that_only_mention_page_slug(app, db, make_user, monkeypatch):
    owner = make_user("canvas_owner", role="editor")
    page = make_page(app, "Team plan", "x")
    decoys = [run(app, lambda i=i: service.create(f"Decoy {i}", "", owner["id"])) for i in range(4)]
    linked = run(app, lambda: service.create("Linked", "", owner["id"]))
    # What a writer may plant: an unresolvable slug, and the page's slug as text, metadata or another node type.
    plants = [
        {"id": "a", "type": "wiki_page", "page_slug": "no-such-page-zq"},
        {"id": "b", "type": "text", "content": page["slug"], "metadata": {"page_slug": page["slug"]}},
        {"id": "c", "type": "text", "page_slug": page["slug"]},
    ]
    for layout in decoys:
        _store(db, layout, _document(*plants))
    # A node saved by an older version (1.4 type name) still carries only the slug.
    _store(db, linked, _document({"id": "old", "type": "page", "page_slug": page["slug"], "label": "stale"}))
    parsed = _parsed_layouts(monkeypatch)

    run(app, lambda: pages.update(pages.get(page["id"]), author_id=None, title="Team plan 2027"))

    assert len(parsed) == 1 and '"old"' in parsed[0]
    node = json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (linked["id"],)))["nodes"][0]
    assert node["page_id"] == page["id"] and "page_slug" not in node and "label" not in node
    assert {db.scalar("SELECT version FROM canvas__layouts WHERE id = ?", (layout["id"],)) for layout in decoys} == {1}


def test_ow2_1_renames_still_follow_escaped_legacy_slugs(app, db, make_user):
    owner = make_user("canvas_owner2", role="editor")
    page = make_page(app, "Città vecchia", "x")
    layout = run(app, lambda: service.create("Legacy", "", owner["id"]))
    raw = json.dumps(_document({"id": "old", "type": "wiki_page", "page_slug": page["slug"]}), ensure_ascii=True)
    assert "\\u" in raw
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (raw, layout["id"]))

    run(app, lambda: pages.change_slug(pages.get(page["id"]), "new-town"))

    node = json.loads(db.scalar("SELECT data FROM canvas__layouts WHERE id = ?", (layout["id"],)))["nodes"][0]
    assert node["page_id"] == page["id"] and "page_slug" not in node


def _vm_steps(fn):
    """Run *fn* in a connection scope; return SQLite's virtual machine steps (in thousands) it took."""
    from bananawiki.wiki.db import db as scoped

    steps = [0]

    def tick():
        steps[0] += 1
        return 0

    def counted():
        scoped.session.conn.set_progress_handler(tick, 1000)
        try:
            fn()
        finally:
            scoped.session.conn.set_progress_handler(None, 0)

    return counted, steps


def test_ow2_1_page_events_do_not_read_canvases_that_only_mention_the_slug(app, db, make_user):
    owner = make_user("canvas_owner3", role="editor")
    page = make_page(app, "Road map", "x")
    decoys = [run(app, lambda i=i: service.create(f"Decoy {i}", "", owner["id"])) for i in range(3)]

    def plant(filler_nodes):
        # The page's slug as text, plus the "page_slug" key on an unresolvable wiki-page node.
        nodes = [{"id": f"f{i}", "type": "text", "content": "x"} for i in range(filler_nodes)]
        nodes += [{"id": "t", "type": "text", "content": page["slug"], "metadata": {"page_slug": page["slug"]}},
                  {"id": "w", "type": "wiki_page", "page_slug": "no-such-page-zq"}]
        for layout in decoys:
            _store(db, layout, _document(*nodes))

    costs = []
    for size, title in ((1, "Road map 2"), (3000, "Road map 3")):
        plant(size)
        counted, steps = _vm_steps(lambda title=title: pages.update(pages.get(page["id"]), author_id=None, title=title))
        run(app, counted)
        costs.append(steps[0])
    # The decoys grew by 9000 nodes; finding the canvases to rewrite must not read them.
    assert costs[1] <= costs[0] + 5, costs


def test_ow2_1_slug_index_follows_every_write_of_a_canvas(app, db, make_user):
    owner = make_user("canvas_owner4", role="editor")
    layout = run(app, lambda: service.create("Indexed", "", owner["id"], data=_document(
        {"id": "w", "type": "wiki_page", "page_slug": "not-yet-written"})))

    def slugs():
        return db.column("SELECT slug FROM canvas__page_slugs WHERE layout_id = ?", (layout["id"],))

    assert slugs() == ["not-yet-written"]
    # Any writer, whatever escapes or old type name it used.
    db.execute("UPDATE canvas__layouts SET data = ? WHERE id = ?", (json.dumps(_document(
        {"id": "a", "type": " Page ", "page_slug": "città"}, {"id": "b", "type": "text", "page_slug": "text-only"},
        {"id": "c", "type": "wiki_page", "page_slug": ""}), ensure_ascii=True), layout["id"]))
    assert slugs() == ["città"]
    db.execute("UPDATE canvas__layouts SET data = '{not json' WHERE id = ?", (layout["id"],))
    assert slugs() == []
    run(app, lambda: service.delete(service.get(layout["id"])))
    assert db.scalar("SELECT COUNT(*) FROM canvas__page_slugs") == 0


def test_ow2_1_upgrade_lists_the_slugs_of_existing_canvases(tmp_path):
    import sqlite3

    from bananawiki.wiki.migrations import v18_canvas_page_slugs

    conn = sqlite3.connect(tmp_path / "v16.db")
    conn.execute("CREATE TABLE canvas__layouts (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO canvas__layouts (id, data) VALUES (1, ?), (2, ?), (3, 'garbage')", (
        json.dumps(_document({"id": "o", "type": "page", "page_slug": "old-home"})),
        json.dumps(_document({"id": "t", "type": "text", "content": "old-home"})),
    ))
    v18_canvas_page_slugs.upgrade(conn)
    v18_canvas_page_slugs.upgrade(conn)
    assert conn.execute("SELECT layout_id, slug FROM canvas__page_slugs").fetchall() == [(1, "old-home")]


# OW3-4: canvas history entry pages are rate limited and render a smaller budget than the live canvas


def _history_canvas(app, client, db, login, make_user):
    db.execute("UPDATE site_settings SET canvas_access = 'all', canvas_write_access = 'editor' WHERE id = 1")
    editor = make_user("history_ed", role="editor")
    login(client, editor)
    assert client.post("/canvas/create", data={"title": "Board"}).status_code == 302
    slug = db.scalar("SELECT slug FROM canvas__layouts")
    notes = [{"id": f"n{i}", "type": "text", "x": i, "y": 0, "content": f"**note {i}** " + "word " * 800}
             for i in range(40)]
    assert client.post(f"/canvas/{slug}/data", json={"data": _document(*notes)}).status_code == 200
    entry = db.scalar("SELECT MAX(id) FROM canvas__history")
    return slug, entry


def _snapshot(response):
    text = response.get_data(as_text=True)
    start = text.index('id="canvas-snapshot">') + len('id="canvas-snapshot">')
    return json.loads(text[start:text.index("</script>", start)])


def test_ow3_4_history_entry_renders_a_bounded_amount_of_the_snapshot(app, client, db, login, make_user):
    slug, entry = _history_canvas(app, client, db, login, make_user)
    present._cache.clear()
    response = client.get(f"/canvas/{slug}/history/{entry}")
    assert response.status_code == 200
    shown = _snapshot(response)
    assert len(shown["data"]["nodes"]) == 40
    rendered = shown["rendered"]
    assert 0 < len(rendered) <= present.HISTORY_RENDER_LIMIT // 4000
    # The live canvas still renders its usual budget.
    assert len(client.get(f"/canvas/{slug}/data").get_json()["rendered"]) == 40


def test_ow3_4_history_entry_views_are_rate_limited(app, client, db, login, make_user):
    slug, entry = _history_canvas(app, client, db, login, make_user)
    codes = [client.get(f"/canvas/{slug}/history/{entry}").status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    codes = [client.get(f"/canvas/{slug}/history").status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
