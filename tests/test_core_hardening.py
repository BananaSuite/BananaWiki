"""Production regressions for bounded requests, files and account deletion."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import quote

import pytest
from filelock import FileLock
from PIL import Image
from werkzeug.datastructures import FileStorage

from bananawiki.core import passwords
from bananawiki.core.ratelimit import SqlLimiter
from bananawiki.wiki import accounts, storage
from bananawiki.wiki.app import MAX_JSON_BYTES
from bananawiki.wiki.db import db as wiki_db
from bananawiki.wiki.features.auth import signin
from tests.features.pages_support import in_app


def test_json_limit_applies_before_csrf_parses_the_body(app, csrf_client, monkeypatch):
    """Untrusted JSON must be bounded before a CSRF token is read from it."""
    parsed = []
    original = app.json.loads

    def loads(value, **kwargs):
        if isinstance(value, bytes) and len(value) > MAX_JSON_BYTES:
            parsed.append(len(value))
        return original(value, **kwargs)

    monkeypatch.setattr(app.json, "loads", loads)
    response = csrf_client.post("/login", json={"padding": "x" * MAX_JSON_BYTES})
    assert response.status_code == 413
    assert parsed == []


@pytest.mark.parametrize("content_length", [None, ""])
def test_chunked_json_cannot_bypass_the_request_limit(admin_client, content_length):
    """A stream without Content-Length still has the same JSON byte budget."""
    body = json.dumps({"content": "ok", "padding": "x" * MAX_JSON_BYTES}).encode()
    response = admin_client.post("/api/preview", data=body, content_type="application/json",
                                 environ_overrides={"CONTENT_LENGTH": content_length, "wsgi.input_terminated": True})
    assert response.status_code == 413


def test_chunked_json_at_the_limit_remains_valid(admin_client):
    overhead = len(json.dumps({"content": "ok", "padding": ""}).encode())
    body = json.dumps({"content": "ok", "padding": "x" * (MAX_JSON_BYTES - overhead)}).encode()
    assert len(body) == MAX_JSON_BYTES
    response = admin_client.post("/api/preview", data=body, content_type="application/json",
                                 environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True})
    assert response.status_code == 200


def test_image_reencoding_must_fit_the_upload_budget(app):
    """A compact low-quality JPEG can grow substantially when metadata is stripped."""
    image = Image.frombytes("RGB", (96, 96), bytes(range(256)) * 108)
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=1, optimize=True)
    data = stream.getvalue()

    def upload():
        storage.save(FileStorage(io.BytesIO(data), filename="small.jpg"), "uploads", allowed=None,
                     max_bytes=len(data), images_only=True)

    with pytest.raises(storage.UploadError) as error:
        in_app(app, upload)
    assert error.value.key == "upload.error.too_large"
    assert in_app(app, lambda: list(storage.folder_path("uploads").iterdir())) == []


def test_upload_quota_checks_files_added_by_other_workers(app):
    """A per-worker usage cache must not allow a new file past the shared quota."""
    def upload():
        used = storage.storage_used(ttl=0)
        app.config["BW"] = replace(app.config["BW"], storage_limit_bytes=used + 20)
        target = storage.folder_path("uploads")
        (target / "another-worker.txt").write_bytes(b"a" * 15)
        storage.save(FileStorage(io.BytesIO(b"b" * 15), filename="mine.txt"), "uploads",
                     allowed={"txt"}, max_bytes=100)

    with pytest.raises(storage.UploadError) as error:
        in_app(app, upload)
    assert error.value.key == "upload.error.quota"
    assert in_app(app, lambda: [p.name for p in storage.folder_path("uploads").iterdir()]) == ["another-worker.txt"]


def test_busy_upload_quota_lock_returns_service_unavailable(app, admin_client, monkeypatch):
    """Concurrent publication waits within a bound and reports a retryable failure."""
    app.config["BW"] = replace(app.config["BW"], storage_limit_bytes=10 * 1024 * 1024)
    path = app.config["BW"].instance_dir + "/.storage-quota.lock"

    def brief_lock(filename, **options):
        assert options["timeout"] == 20
        options["timeout"] = 0.01
        return FileLock(filename, **options)

    monkeypatch.setattr(storage, "FileLock", brief_lock)
    image = io.BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="PNG")
    image.seek(0)
    with FileLock(path, timeout=0, mode=0o600):
        response = admin_client.post("/api/upload", data={"file": (image, "image.png")},
                                     content_type="multipart/form-data")
    assert response.status_code == 503
    assert in_app(app, lambda: list(storage.folder_path("uploads").iterdir())) == []


@pytest.mark.parametrize("filename", ["embedded\x00null.png", "x" * 300 + ".png"])
def test_invalid_storage_filenames_are_missing_files(admin_client, filename):
    response = admin_client.get("/static/uploads/" + quote(filename, safe=""))
    assert response.status_code == 404


def test_account_deletion_checks_current_owner_role(app, db, make_user):
    """The target may have become the last owner since a request loaded it."""
    user = make_user("new_owner")
    db.execute("UPDATE users SET role = 'owner' WHERE id = ?", (user["id"],))
    with pytest.raises(accounts.AccountError) as error:
        in_app(app, lambda: accounts.delete(user))
    assert error.value.key == "admin.users.error.last_owner"
    assert db.scalar("SELECT COUNT(*) FROM users WHERE role = 'owner'") == 1


def test_account_deletion_rechecks_owner_count_after_acquiring_write_lock(app, db, make_user):
    """Another deletion can commit between the first read and the write lock."""
    first = make_user("first_owner", role="owner")
    other = make_user("other_owner", role="owner")

    def remove():
        original = wiki_db.session.transaction

        @contextmanager
        def transaction():
            db.execute("DELETE FROM users WHERE id = ?", (other["id"],))
            with original():
                yield

        wiki_db.session.transaction = transaction
        accounts.delete(first)

    with pytest.raises(accounts.AccountError) as error:
        in_app(app, remove)
    assert error.value.key == "admin.users.error.last_owner"
    assert db.scalar("SELECT COUNT(*) FROM users WHERE role = 'owner'") == 1


def test_concurrent_password_checks_reserve_the_remaining_login_budget(app, make_user, monkeypatch):
    """A guess being hashed already occupies the last available attempt."""
    user = make_user("login_target")

    def seed():
        limiter = SqlLimiter(wiki_db.session)
        for _ in range(signin.MAX_PER_ACCOUNT - 1):
            limiter.record(f"{user['username']}\nunknown", "login:account-ip")

    in_app(app, seed)
    entered, release = threading.Event(), threading.Event()
    original = passwords.verify_password

    def verify(stored, password):
        if password == "first-invalid-guess":
            entered.set()
            assert release.wait(5), "parallel verification did not finish"
        return original(stored, password)

    monkeypatch.setattr(passwords, "verify_password", verify)

    def attempt(password):
        try:
            in_app(app, lambda: signin.verify(user["username"], password))
        except signin.Refused as error:
            return error.status
        raise AssertionError("an invalid password authenticated")

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(attempt, "first-invalid-guess")
        try:
            assert entered.wait(5)
            second = attempt("second-invalid-guess")
        finally:
            release.set()
        assert first.result(timeout=5) == 401
    assert second == 429


def test_successful_login_refunds_only_its_aggregate_reservations(app, make_user):
    from tests.conftest import PASSWORD

    user = make_user("valid_target")

    def verify():
        limiter = SqlLimiter(wiki_db.session)
        previous_ip = limiter.record("unknown", "login:ip")
        previous_account = limiter.record(user["username"], "login:account")
        assert signin.verify(user["username"], PASSWORD)["id"] == user["id"]
        rows = wiki_db.all("SELECT id FROM rate_limit_hits")
        assert {row["id"] for row in rows} == {previous_ip, previous_account}

    in_app(app, verify)
