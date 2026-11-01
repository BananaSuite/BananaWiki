"""Regression tests for the s3 findings of the full re-audit pass 13 (memory the bulk Markdown
import holds as decoded text, and audit entries for deleting a Kanban board or canvas or its history)."""

from __future__ import annotations

import io
import json
import sys
import zipfile

from bananawiki.wiki.features.page_export import bulk
from bananawiki.wiki.features.pages import service as pages

# SW2-4: the bound of the bulk Markdown import counts the memory the decoded texts take

_ASTRAL = "\U0001F600"  # one character above U+FFFF makes CPython store 4 bytes per character


def _astral_text(name: str, size: int) -> bytes:
    head = f"# {name} {_ASTRAL}\n\n".encode()
    return head + b"a" * (size - len(head))


def test_sw2_4_bulk_import_counts_decoded_text_against_the_bound(app, admin_client, monkeypatch):
    monkeypatch.setattr(bulk, "MAX_TOTAL_BYTES", 20_000)
    held: list[int] = []
    real = bulk.import_files
    monkeypatch.setattr(bulk, "import_files", lambda files, **kw: (
        held.append(sum(sys.getsizeof(t) for _, t in files)), real(files, **kw))[1])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in range(3):
            zf.writestr(f"zipped-{n}.md", _astral_text(f"Zipped {n}", 2_000))
    archive.seek(0)
    # 3 x 2,000 bytes in an archive, then 4 x 2,000 loose: 14,000 bytes, under the bound in bytes,
    # but each text takes about 8,000 bytes once decoded.
    data = {"import_file": [(archive, "zipped.zip")]
            + [(io.BytesIO(_astral_text(f"Loose {n}", 2_000)), f"loose-{n}.md") for n in range(4)]}
    response = admin_client.post("/admin/bulk-markdown", data=data, content_type="multipart/form-data",
                                 follow_redirects=True)
    assert response.status_code == 200
    assert held and held[0] <= 20_000
    text = response.get_data(as_text=True)
    assert "loose-2.md is too large once unpacked." in text and "zipped.zip is too large once unpacked." in text
    with app.app_context():
        assert pages.get_by_slug("loose-0") is not None and pages.get_by_slug("loose-1") is not None
        assert pages.get_by_slug("loose-2") is None and pages.get_by_slug("zipped-0") is None


# SW3-4: deleting a Kanban board or canvas, and deleting or clearing its history, is audited


def _private_board_and_canvas(app, db, make_user, login):
    from bananawiki.wiki import registry
    from bananawiki.wiki.db import connection_scope

    with app.test_request_context(), connection_scope():
        for feature in ("audit", "kanban", "canvas"):
            registry.set_enabled(feature, True)
    owner = app.test_client()
    login(owner, make_user("owner1", role="owner"))
    assert owner.post("/kanban/create", data={"title": "Secret board", "description": ""}).status_code == 302
    board_id = db.scalar("SELECT id FROM kanban_boards WHERE title = 'Secret board'")
    owner.post(f"/kanban/{board_id}/share", data={"action": "set_visibility", "visibility": "private"})
    owner.post(f"/api/kanban/{board_id}/columns", json={"title": "Todo"})
    owner.post(f"/api/kanban/{board_id}/columns", json={"title": "Doing"})
    assert owner.post("/canvas/create", data={"title": "Secret canvas"}).status_code == 302
    slug, canvas_id = db.one("SELECT slug, id FROM canvas__layouts WHERE title = 'Secret canvas'").values()
    for message in ("one", "two"):
        db.execute("INSERT INTO canvas__history (layout_id, title, description, data, edited_by, edit_message, "
                   "is_revert, created_at) SELECT id, title, description, data, creator_id, ?, 0, "
                   "CURRENT_TIMESTAMP FROM canvas__layouts WHERE id = ?", (message, canvas_id))
    admin = app.test_client()
    login(admin, make_user("adm", role="admin"))
    return board_id, slug, canvas_id, admin


def test_sw3_4_admin_deleting_a_board_or_canvas_or_its_history_is_audited(app, db, make_user, login):
    board_id, slug, canvas_id, admin = _private_board_and_canvas(app, db, make_user, login)
    admin_id = db.scalar("SELECT id FROM users WHERE username = 'adm'")
    start = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    board_entries = db.all("SELECT id FROM kanban_board_history WHERE board_id = ? ORDER BY id", (board_id,))
    canvas_entries = db.all("SELECT id FROM canvas__history WHERE layout_id = ? ORDER BY id", (canvas_id,))
    assert len(board_entries) >= 2 and len(canvas_entries) >= 2
    for url in (f"/kanban/{board_id}/history/{board_entries[0]['id']}/delete", f"/kanban/{board_id}/history/clear",
                f"/canvas/{slug}/history/{canvas_entries[0]['id']}/delete", f"/canvas/{slug}/history/clear",
                f"/kanban/{board_id}/delete", f"/canvas/{slug}/delete"):
        assert admin.post(url).status_code == 302, url
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards WHERE id = ?", (board_id,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM canvas__layouts WHERE id = ?", (canvas_id,)) == 0
    rows = db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? ORDER BY id",
                  (start,))
    assert [row["action"] for row in rows] == [
        "kanban.history_entry_deleted", "kanban.history_cleared", "canvas.history_entry_deleted",
        "canvas.history_cleared", "kanban.board_deleted", "canvas.deleted"]
    assert all(row["actor_id"] == admin_id for row in rows)
    details = [json.loads(row["details"]) for row in rows]
    assert details[0]["title"] == "Secret board" and details[0]["creator"] == "owner1"
    assert details[1]["count"] == len(board_entries) - 1 and details[3]["count"] == len(canvas_entries) - 1
    assert details[4] == {"title": "Secret board", "creator": "owner1", "visibility": "private"}
    assert rows[5]["target_id"] == slug and details[5]["title"] == "Secret canvas"
    assert details[5]["creator"] == "owner1"
