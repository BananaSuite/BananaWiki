"""Uploaded JSON documents use the same safe boundary as API request bodies."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from werkzeug.datastructures import FileStorage

from bananawiki.wiki.features.site_admin import appearance, languages


@pytest.mark.parametrize("feature, table, member", [("canvas", "canvas__layouts", "canvas.json"),
                                                   ("kanban", "kanban_boards", "board.json")])
@pytest.mark.parametrize("as_zip", [False, True], ids=["plain", "zip"])
@pytest.mark.parametrize("bad", ["\ud800", "\udfff", float("inf")], ids=["high-surrogate", "low-surrogate", "infinity"])
def test_uploaded_document_cannot_reach_storage_with_unsafe_json(admin_client, db, feature, table, member,
                                                               as_zip, bad):
    document = {"title": "Imported board", "columns": [], "data": {"nodes": [], "edges": []},
                "ignored": {"value": bad}}
    body = json.dumps(document).encode()
    if as_zip:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(member, body)
        body = archive.getvalue()
    before = db.scalar(f"SELECT COUNT(*) FROM {table}")
    response = admin_client.post(f"/{feature}/import", data={
        "import_file": (io.BytesIO(body), f"board.{feature}.{'zip' if as_zip else 'json'}")},
        content_type="multipart/form-data")
    assert response.status_code == 302
    assert db.scalar(f"SELECT COUNT(*) FROM {table}") == before


@pytest.mark.parametrize("reader, error, name", [(languages.read_upload, languages.LanguageError, "language.json"),
                                               (appearance.parse_theme_file, appearance.AppearanceError, "theme.bwtheme")])
def test_deeply_nested_uploaded_configuration_is_a_normal_validation_error(reader, error, name):
    body = b'{"ignored":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    with pytest.raises(error):
        reader(FileStorage(io.BytesIO(body), filename=name))
