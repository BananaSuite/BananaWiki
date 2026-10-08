"""Kanban work bounded by the server, not by what a client sends: bulk ids, orderings, export and import."""

from __future__ import annotations

import dataclasses
import io
import json
import struct
import tempfile
import types
import zipfile

import pytest

from bananawiki.wiki import storage
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.kanban import history, service, store, transfer
from bananawiki.wiki.features.kanban.fields import KanbanError

from .api_support import call, enable_api, issue
from .pages_support import in_app
from .test_kanban import board_id_from, columns, new_ticket


@pytest.fixture
def board(admin_client):
    return board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))


class _Counted:
    """An id that counts how often it is read."""

    reads = 0

    def __int__(self) -> int:
        _Counted.reads += 1
        return 1


# ── Bulk selections ──────────────────────────────────────────────────────────


def test_bulk_selection_over_the_limit_is_refused_before_reading_it():
    _Counted.reads = 0
    with pytest.raises(KanbanError) as refused:
        service.tickets_of_board({"id": 1}, [_Counted()] * (service.BULK_LIMIT + 1))
    assert refused.value.key == "kanban.error.too_many" and _Counted.reads == 0
    with pytest.raises(KanbanError) as refused:
        service.columns_of_board({"id": 1}, [1] * (service.BULK_LIMIT + 1))
    assert refused.value.key == "kanban.error.too_many"


def test_bulk_requests_count_every_id_sent(admin_client, board, db):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    url = f"/api/kanban/{board}/tickets/bulk"
    repeated = [ticket["id"]] * (service.BULK_LIMIT + 1)
    response = admin_client.post(url, json={"ticket_ids": repeated, "action": "priority", "priority": "low"})
    assert response.status_code == 400
    assert db.scalar("SELECT priority FROM kanban_tickets WHERE id = ?", (ticket["id"],)) == "medium"
    response = admin_client.post(url, json={"ticket_ids": [ticket["id"]] * 3, "action": "priority", "priority": "low"})
    assert response.get_json() == {"updated": 1}
    column = columns(admin_client, board)[1]["id"]
    response = admin_client.post(f"/api/kanban/{board}/columns/bulk",
                                 json={"action": "delete", "column_ids": [column] * (service.BULK_LIMIT + 1)})
    assert response.status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM kanban_columns WHERE id = ?", (column,)) == 1


def test_api_bulk_archive_refuses_oversized_selections(app, client, db, admin):
    enable_api(app, db)
    created = in_app(app, lambda: service.create_board(admin, "Roadmap"))
    ticket = in_app(app, lambda: service.create_ticket(created, store.columns_of(created["id"])[0], admin,
                                                       {"title": "Task"}))
    token = issue(app, admin, ["kanban"])
    url = f"/kanban/boards/{created['id']}/tickets/archive"
    refused = call(client, "POST", url, token, json={"ids": [ticket["id"]] * (service.BULK_LIMIT + 1)})
    assert refused.status_code == 400 and refused.json["code"] == "too_many"
    assert db.scalar("SELECT archived_at FROM kanban_tickets WHERE id = ?", (ticket["id"],)) is None
    archived = call(client, "POST", url, token, json={"ids": [ticket["id"], str(ticket["id"])]})
    assert archived.status_code == 200 and archived.json["archived"] == 1


def test_repeated_assignees_count_once_in_the_order_sent(monkeypatch):
    monkeypatch.setattr(service.access, "assignable_ids", lambda board: {"a", "b", "c"})
    sent = ["b", "a", " b ", "", None, "x", "a", "c"] * 1000
    assert service._assignees({"id": 1}, sent, strict=False) == ["b", "a", "c"]
    with pytest.raises(KanbanError):
        service._assignees({"id": 1}, sent, strict=True)


# ── Orderings ────────────────────────────────────────────────────────────────


def test_reorder_reads_a_bounded_part_of_the_wanted_order():
    consumed = 0

    def wanted():
        nonlocal consumed
        for value in [3, "x", 1, 3, 99] + [7] * 100_000:
            consumed += 1
            yield value

    assert store.reorder([1, 2, 3], wanted()) == [3, 1, 2]
    assert consumed < 1000  # not the 100 000 copies
    assert store.reorder([1, 2, 3], [2, 2, 1]) == [2, 1, 3]
    assert store.reorder([], [1, 2]) == []


def test_saved_orders_are_read_whole(app, admin_client, db, make_user, monkeypatch):
    """Only orders a client sends are cut short; a saved order may name many boards one cannot see."""
    monkeypatch.setattr(store, "REORDER_SLACK", 0)
    db.execute("UPDATE site_settings SET kanban_open_access = 1 WHERE id = 1")
    created = {title: board_id_from(admin_client.post("/kanban/create", data={"title": title}))
               for title in ("V1", "V2", "P1", "P2")}
    for title in ("P1", "P2"):
        admin_client.post(f"/kanban/{created[title]}/share", data={"action": "set_visibility",
                                                                  "visibility": "private"})
    order = [created[title] for title in ("P1", "P2", "V1", "V2")]
    admin_client.post("/api/kanban/board-order", json={"board_ids": order})
    reader = make_user("reader", role="editor")
    listed = in_app(app, lambda: [row["id"] for row in service.ordered_boards(reader)])
    assert listed == [created["V1"], created["V2"]]


def test_reorder_endpoint_ignores_a_padded_order(admin_client, board):
    column = columns(admin_client, board)[0]["id"]
    first, second = (new_ticket(admin_client, column, title)["id"] for title in ("A", "B"))
    padded = [second, first] + [0] * 20_000
    response = admin_client.post(f"/api/kanban/columns/{column}/tickets/reorder", json={"order": padded})
    assert response.get_json()["order"] == [second, first]


# ── Export ───────────────────────────────────────────────────────────────────


def _attach(client, ticket_id, name, data):
    response = client.post(f"/api/kanban/tickets/{ticket_id}/attachments", data={"file": (io.BytesIO(data), name)},
                           content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()


def test_zip_export_is_written_to_a_file_and_stores_compressed_formats(app, admin_client, board):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    _attach(admin_client, ticket["id"], "notes.txt", b"plain text " * 100)
    _attach(admin_client, ticket["id"], "song.mp3", bytes(range(256)) * 8)
    with app.test_request_context(), connection_scope():
        row = store.get_board(board)
        archive = transfer.export_zip(*transfer.export(row))
    try:
        assert not isinstance(archive, io.BytesIO) and archive.fileno() >= 0
        with zipfile.ZipFile(archive) as bundled:
            methods = {info.filename.rsplit(".", 1)[-1]: info.compress_type for info in bundled.infolist()}
    finally:
        archive.close()
    assert methods == {"json": zipfile.ZIP_DEFLATED, "txt": zipfile.ZIP_DEFLATED, "mp3": zipfile.ZIP_STORED}
    response = admin_client.get(f"/kanban/{board}/export")
    assert response.mimetype == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(response.data)).testzip() is None


def test_zip_export_the_import_would_refuse_is_not_built(admin_client, board, monkeypatch):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    _attach(admin_client, ticket["id"], "notes.txt", b"x" * 4096)
    monkeypatch.setattr(transfer, "MAX_UNCOMPRESSED", 2048)
    response = admin_client.get(f"/kanban/{board}/export")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/kanban/{board}")
    page = admin_client.get(f"/kanban/{board}").get_data(as_text=True)
    assert "without them from the More menu" in page and f"/kanban/{board}/export?format=json" in page
    assert admin_client.get(f"/kanban/{board}/export?format=json").mimetype == "application/json"
    monkeypatch.setattr(transfer, "MAX_UNCOMPRESSED", 1024 * 1024)
    monkeypatch.setattr(transfer, "MAX_MEMBERS", 2)
    assert admin_client.get(f"/kanban/{board}/export").status_code == 200
    _attach(admin_client, ticket["id"], "more.txt", b"y")
    assert admin_client.get(f"/kanban/{board}/export").status_code == 302


# ── Import ───────────────────────────────────────────────────────────────────


def _zip(*members: tuple, comment: bytes = b"") -> bytes:
    """A ZIP of ``(name, data)`` or ``(name, data, compression)`` members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data, *method in members:
            bundle.writestr(name, data, compress_type=method[0] if method else None)
        bundle.comment = comment
    return buffer.getvalue()


def _document(*bundled: str, padding: str = "") -> str:
    """A ``board.json`` whose only ticket refers to the *bundled* paths."""
    attachments = [{"bundle_path": path, "original_name": path.rsplit("/", 1)[-1]} for path in bundled]
    return json.dumps({"title": "Imported", "description": padding,
                       "columns": [{"title": "To do", "tickets": [{"title": "T", "attachments": attachments}]}]})


def _import(client, body: bytes):
    return client.post("/kanban/import", data={"import_file": (io.BytesIO(body), "board.kanban.zip")},
                       content_type="multipart/form-data")


def _attachment_names(db) -> list[str]:
    return db.column("SELECT original_name FROM kanban_ticket_attachments ORDER BY id")


@pytest.fixture
def opened(monkeypatch):
    """Names of the archive members the import unpacks."""
    names: list[str] = []
    original = zipfile.ZipFile.open

    def spy(self, name, mode="r", *args, **kwargs):
        if mode == "r":
            names.append(getattr(name, "filename", name))
        return original(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    return names


def test_board_json_in_a_zip_is_held_to_the_json_limit(admin_client, db, monkeypatch, opened):
    monkeypatch.setattr(transfer, "MAX_JSON", 2000)
    _import(admin_client, _zip(("board.json", _document(padding="x" * 3000))))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0 and opened == []
    _import(admin_client, _zip(("board.json", _document(padding="x" * 100))))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1


def test_only_referenced_attachments_are_unpacked_and_streamed(admin_client, db, monkeypatch, opened):
    streams: list[str] = []
    save = storage.save

    def spy(upload, *args, **kwargs):
        streams.append(type(upload.stream).__name__)
        return save(upload, *args, **kwargs)

    monkeypatch.setattr(storage, "save", spy)
    body = _zip(("board.json", _document("attachments/1/a.txt")), ("attachments/1/a.txt", b"kept"),
                ("attachments/9/unused.txt", b"never read" * 1000))
    assert _import(admin_client, body).status_code == 302
    assert _attachment_names(db) == ["a.txt"]
    assert "attachments/9/unused.txt" not in opened
    assert streams == ["ZipExtFile"]


@pytest.mark.parametrize("member", [
    ("attachments/1/zeros.txt", bytes(2 * 1024 * 1024)),
    ("attachments/1/zeros.txt", b"bzip2 data", zipfile.ZIP_BZIP2),
], ids=["ratio", "bzip2"])
def test_tightly_packed_or_unusual_attachments_make_the_archive_invalid(admin_client, db, member):
    _import(admin_client, _zip(("board.json", _document(member[0])), member))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0
    # Not referenced, the same member is never unpacked.
    _import(admin_client, _zip(("board.json", _document()), member))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1


def test_attachments_beyond_the_storage_left_are_skipped_unread(app, admin_client, db, monkeypatch, opened):
    limit = 10 ** 9
    monkeypatch.setitem(app.config, "BW", dataclasses.replace(app.config["BW"], storage_limit_bytes=limit))
    monkeypatch.setattr(storage, "storage_used", lambda ttl=60.0: limit - 50)
    body = _zip(("board.json", _document("attachments/1/big.txt", "attachments/1/small.txt")),
                ("attachments/1/big.txt", b"b" * 100), ("attachments/1/small.txt", b"s" * 10))
    assert _import(admin_client, body).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1
    assert _attachment_names(db) == ["small.txt"]
    assert "attachments/1/big.txt" not in opened


@pytest.fixture
def listed(monkeypatch):
    """One entry each time zipfile reads an archive's central directory."""
    calls: list[bool] = []
    original = zipfile.ZipFile._RealGetContents

    def spy(self):
        calls.append(True)
        return original(self)

    monkeypatch.setattr(zipfile.ZipFile, "_RealGetContents", spy)
    return calls


def test_an_oversized_central_directory_is_refused_before_it_is_listed(admin_client, db, monkeypatch, listed):
    monkeypatch.setattr(transfer, "MAX_MEMBERS", 4)
    many = [(f"attachments/{index}/padding-name-{index:04}.txt", b"") for index in range(40)]
    _import(admin_client, _zip(("board.json", _document()), *many))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0 and listed == []
    _import(admin_client, _zip(("board.json", _document())))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1


def _hidden_directory(entries: int) -> bytes:
    """A ZIP whose plain end record announces an empty directory, while the ZIP64 record it
    leads to, followed by extensible data, announces *entries* more."""
    real = _zip(("board.json", _document()))
    start = zipfile._EndRecData(io.BytesIO(real))[zipfile._ECD_OFFSET]
    entry = struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0) + b"a"
    directory = real[start:real.rfind(b"PK\x05\x06")] + entry * entries
    extensible = b"data"
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44 + len(extensible), 45, 45, 0, 0, entries + 1, entries + 1,
                         len(directory), start) + extensible
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, start + len(directory), 1)
    return real[:start] + directory + record + locator + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 0)


def test_a_directory_announced_only_by_its_zip64_record_is_refused_before_it_is_listed(admin_client, db,
                                                                                         monkeypatch, listed):
    monkeypatch.setattr(transfer, "MAX_MEMBERS", 4)
    body = _hidden_directory(60)
    if zipfile._EndRecData(io.BytesIO(body))[zipfile._ECD_SIZE] == 0:
        pytest.skip("this zipfile does not read a ZIP64 record followed by extensible data")
    assert body.startswith(b"PK\x03\x04") and len(zipfile.ZipFile(io.BytesIO(body)).infolist()) == 61
    listed.clear()
    _import(admin_client, body)
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0 and listed == []
    with monkeypatch.context() as patch:  # a small archive with a ZIP64 end record is fine
        patch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        small = _zip(("board.json", _document()), ("attachments/1/a.txt", b"a"))
    assert b"PK\x06\x06" in small
    _import(admin_client, small)
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1


@pytest.mark.parametrize("missing", ["_EndRecData", "_ECD_SIZE"])
def test_zips_still_import_where_zipfile_lacks_a_name_the_directory_check_uses(admin_client, db, monkeypatch,
                                                                               missing):
    without = types.ModuleType("zipfile")  # zipfile as a future Python might have it
    vars(without).update({key: value for key, value in vars(zipfile).items() if key != missing})
    monkeypatch.setattr(transfer, "zipfile", without)
    _import(admin_client, _zip(("board.json", _document())))
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 1


def _header(body: bytearray, name: str, *, central: bool) -> int:
    """Where the local or the central directory header of *name* starts."""
    signature, length_at, name_at = (b"PK\x01\x02", 28, 46) if central else (b"PK\x03\x04", 26, 30)
    position = body.index(signature)
    while True:
        length = int.from_bytes(body[position + length_at:position + length_at + 2], "little")
        if body[position + name_at:position + name_at + length] == name.encode():
            return position
        position = body.index(signature, position + 4)


def _damage(body: bytearray, name: str, damage: str) -> bytearray:
    """*body* with the entry of *name* changed the way a crafted archive could change it."""
    if damage == "utf8_name":  # flagged UTF-8, but the local name is not
        local = _header(body, name, central=False)
        body[local + 7] |= 0x08  # bit 11 of the flags
        body[local + 30 + len(name) - 1] = 0xFF
        return body
    central = _header(body, name, central=True)
    if damage == "version":
        body[central + 6] = 99  # needs a newer zip version than any reader knows
    elif damage == "huge_offset":  # a ZIP64 field places the local header past any offset a file can seek to
        extra = struct.pack("<2HQ", 1, 8, 2 ** 64 - 1)
        at = central + 46 + len(name)
        body[at:at] = extra
        body[central + 30:central + 32] = (int.from_bytes(body[central + 30:central + 32], "little")
                                           + len(extra)).to_bytes(2, "little")
        body[central + 42:central + 46] = b"\xff" * 4
        end = body.rindex(b"PK\x05\x06")
        size = int.from_bytes(body[end + 12:end + 16], "little")
        body[end + 12:end + 16] = (size + len(extra)).to_bytes(4, "little")
    else:
        body[central + 8] |= {"patched": 0x20, "strong_encryption": 0x40}[damage]
    return body


@pytest.mark.parametrize("damage", ["utf8_name", "patched", "strong_encryption", "version", "huge_offset"])
@pytest.mark.parametrize("target", ["board.json", "attachments/1/b.txt"])
def test_crafted_entries_make_the_archive_invalid(app, admin_client, db, target, damage):
    folder = in_app(app, lambda: storage.folder_path(store.FOLDER))
    before = set(folder.iterdir()) if folder.is_dir() else set()
    body = bytearray(_zip(("board.json", _document("attachments/1/a.txt", "attachments/1/b.txt")),
                          ("attachments/1/a.txt", b"saved first"), ("attachments/1/b.txt", b"then refused")))
    body = _damage(body, target, damage)
    if damage == "huge_offset":
        assert zipfile.ZipFile(io.BytesIO(body)).getinfo(target).header_offset == 2 ** 64 - 1
    response = _import(admin_client, bytes(body))
    assert response.status_code == 302 and response.headers["Location"].endswith("/kanban")
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0 and _attachment_names(db) == []
    assert (set(folder.iterdir()) if folder.is_dir() else set()) == before


def _before_the_start(body: bytearray, name: str) -> bytearray:
    """*body* with the end record's directory offset and every header offset but that of *name*
    moved past the end of the archive, so that zipfile places *name* before the archive starts."""
    shift = len(body)
    end = body.rindex(b"PK\x05\x06")
    start = int.from_bytes(body[end + 16:end + 20], "little")
    body[end + 16:end + 20] = (start + shift).to_bytes(4, "little")
    position = start
    while body[position:position + 4] == b"PK\x01\x02":
        lengths = struct.unpack("<3H", body[position + 28:position + 34])
        if body[position + 46:position + 46 + lengths[0]] != name.encode():
            offset = int.from_bytes(body[position + 42:position + 46], "little")
            body[position + 42:position + 46] = (offset + shift).to_bytes(4, "little")
        position += 46 + sum(lengths)
    return body


@pytest.mark.parametrize("upload", ["in_memory", "spooled", "file"])
@pytest.mark.parametrize("target", ["board.json", "attachments/1/b.txt"])
def test_entries_placed_before_the_archive_make_it_invalid(app, admin_client, db, monkeypatch, target, upload):
    if upload == "file":  # on disk whatever its size, as Werkzeug spools a large upload
        monkeypatch.setattr(app.request_class, "_get_file_stream",  # the request closes the file
                            lambda self, *args, **kwargs: tempfile.TemporaryFile("rb+"))  # noqa: SIM115
    folder = in_app(app, lambda: storage.folder_path(store.FOLDER))
    before = set(folder.iterdir()) if folder.is_dir() else set()
    # Werkzeug keeps an upload in memory up to 500 KiB and spools a larger one to a file.
    padding = [("attachments/9/padding.bin", bytes(600 * 1024), zipfile.ZIP_STORED)] if upload == "spooled" else []
    body = _before_the_start(bytearray(_zip(
        ("board.json", _document("attachments/1/a.txt", "attachments/1/b.txt")),
        ("attachments/1/a.txt", b"saved first"), *padding, ("attachments/1/b.txt", b"then refused"))), target)
    assert (len(body) > 500 * 1024) is (upload == "spooled")
    assert zipfile.ZipFile(io.BytesIO(body)).getinfo(target).header_offset < 0
    response = _import(admin_client, bytes(body))
    assert response.status_code == 302 and response.headers["Location"].endswith("/kanban")
    assert db.scalar("SELECT COUNT(*) FROM kanban_boards") == 0 and _attachment_names(db) == []
    assert (set(folder.iterdir()) if folder.is_dir() else set()) == before


# ── History ──────────────────────────────────────────────────────────────────


def _entries(db, board_id: int) -> list[dict]:
    return db.all("SELECT id, length(snapshot) AS size FROM kanban_board_history WHERE board_id = ? ORDER BY id",
                  (board_id,))


def test_board_history_is_bounded_by_bytes(admin_client, board, db, monkeypatch):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"], description="d" * 5000)
    sizes = [entry["size"] for entry in _entries(db, board)]
    monkeypatch.setattr(history, "HISTORY_MAX_BYTES", 3 * max(sizes) + 100, raising=False)
    for index in range(8):
        admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"title": f"Title {index}"})
    kept = _entries(db, board)
    assert 1 < len(kept) <= 3 and sum(entry["size"] for entry in kept) <= history.HISTORY_MAX_BYTES
    newest = kept[-1]["id"]
    monkeypatch.setattr(history, "HISTORY_MAX_BYTES", 1, raising=False)
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"title": "Last"})
    remaining = _entries(db, board)
    assert len(remaining) == 1 and remaining[0]["id"] > newest  # the newest entry always stays
    snapshot = db.scalar("SELECT snapshot FROM kanban_board_history WHERE id = ?", (remaining[0]["id"],))
    assert history.decode(snapshot)["columns"][0]["tickets"][0]["title"] == "Last"


def test_moving_a_ticket_back_and_forth_shares_one_entry(admin_client, board, db):
    todo, doing, done = (column["id"] for column in columns(admin_client, board))
    ticket = new_ticket(admin_client, todo)
    before = len(_entries(db, board))
    for target in (doing, todo, done, doing):
        admin_client.post(f"/api/kanban/tickets/{ticket['id']}/move", json={"column_id": target})
    assert len(_entries(db, board)) == before + 1
    activity = db.column("SELECT details FROM kanban_activity_log WHERE board_id = ? AND action = 'ticket_moved' "
                         "ORDER BY id", (board,))
    assert len(activity) == 4 and activity[-1].endswith("“In progress”")
    other = new_ticket(admin_client, todo, "Other")
    admin_client.post(f"/api/kanban/tickets/{other['id']}/move", json={"column_id": done})
    assert len(_entries(db, board)) == before + 3  # created, then moved: another ticket, another entry


def test_ticket_description_history_is_bounded(admin_client, board, db, monkeypatch):
    ticket = new_ticket(admin_client, columns(admin_client, board)[0]["id"])
    monkeypatch.setattr(history, "TICKET_HISTORY_KEPT", 3, raising=False)
    for index in range(6):
        admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"description": f"version {index}"})
    kept = db.column("SELECT new_description FROM kanban_ticket_history WHERE ticket_id = ? ORDER BY id",
                     (ticket["id"],))
    assert kept == ["version 3", "version 4", "version 5"]
    monkeypatch.setattr(history, "TICKET_HISTORY_MAX_BYTES", 10, raising=False)
    admin_client.put(f"/api/kanban/tickets/{ticket['id']}", json={"description": "x" * 50})
    kept = db.column("SELECT new_description FROM kanban_ticket_history WHERE ticket_id = ?", (ticket["id"],))
    assert kept == ["x" * 50]
