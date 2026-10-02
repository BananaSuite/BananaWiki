"""Malformed CSRF input and simultaneous file access must fail predictably."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from werkzeug.exceptions import NotFound

from bananawiki.wiki import storage
from bananawiki.wiki.db import connection_scope


@pytest.mark.parametrize("token", ["\ud800", "\udfff"])
def test_invalid_unicode_csrf_token_is_a_bad_request(csrf_client, token):
    csrf_client.get("/login")
    response = csrf_client.post("/login", json={"csrf_token": token})
    assert response.status_code == 400


def test_simultaneous_legacy_blob_restores_publish_complete_files(app, db, monkeypatch):
    content = b"legacy attachment contents" * 1000
    filename = "0" * 32 + ".txt"
    blob_id = db.insert("file_blobs", {"filename": filename, "content": content})
    with app.app_context():
        folder = storage.folder_path("attachments")
    ready = Barrier(2)
    replace = storage.os.replace

    def simultaneous_publish(source, target):
        if target == folder / filename:
            ready.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(storage.os, "replace", simultaneous_publish)

    def restore():
        with app.test_request_context(), connection_scope():
            return storage.resolve("attachments", filename, blob_id=blob_id).read_bytes()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(restore) for _ in range(2)]
        assert [future.result(timeout=10) for future in pending] == [content, content]
    assert [path.name for path in folder.iterdir()] == [filename]


def test_file_deleted_between_resolution_and_open_is_missing(app, monkeypatch):
    with app.test_request_context(), connection_scope():
        folder = storage.folder_path("attachments")
        path = folder / "deleted.txt"
        path.write_bytes(b"download")
        resolve = storage.resolve

        def removed_after_resolution(*args, **options):
            result = resolve(*args, **options)
            path.unlink()
            return result

        monkeypatch.setattr(storage, "resolve", removed_after_resolution)
        with pytest.raises(NotFound):
            storage.send("attachments", path.name)
