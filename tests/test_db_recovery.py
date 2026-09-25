"""Database faults must preserve data and require deliberate recovery."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

import config
import db


def test_corrupt_database_is_preserved_during_concurrent_connections(tmp_path, monkeypatch):
    damaged = tmp_path / "damaged.db"
    content = b"damaged original database bytes " * 200
    damaged.write_bytes(content)
    with monkeypatch.context() as patch:
        patch.setattr(config, "DATABASE_PATH", str(damaged))
        def request_connection(_):
            with pytest.raises(sqlite3.DatabaseError):
                db.get_db()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(request_connection, range(24)))
        with pytest.raises(sqlite3.DatabaseError):
            db.init_db()
        assert db.integrity_check() != "ok"
    assert damaged.read_bytes() == content
    assert not (tmp_path / "corrupt").exists()
    assert not Path(str(damaged) + ".initialized").exists()


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_truncated_initialized_database_is_never_recreated(tmp_path, monkeypatch, missing):
    target = tmp_path / "previously-running.db"
    with monkeypatch.context() as patch:
        patch.setattr(config, "DATABASE_PATH", str(target))
        db.init_db()
        assert Path(str(target) + ".initialized").is_file()
        if missing:
            target.unlink()
        else:
            target.write_bytes(b"")
        with pytest.raises(sqlite3.DatabaseError, match="missing or empty"):
            db.get_db()
        with pytest.raises(sqlite3.DatabaseError, match="missing or empty"):
            db.init_db()
        assert db.integrity_check() != "ok"
    assert not target.exists() if missing else target.read_bytes() == b""


@pytest.mark.parametrize("accept", ["text/html", "application/json"])
def test_corruption_returns_an_outage_without_reinitializing_setup(client, tmp_path, monkeypatch, accept):
    damaged = tmp_path / "private-storage-name.db"
    damaged.write_bytes(b"corrupt customer storage" * 200)
    with monkeypatch.context() as patch:
        patch.setattr(config, "DATABASE_PATH", str(damaged))
        response = client.get("/", headers={"Accept": accept})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert "no-store" in response.headers["Cache-Control"]
    assert b"temporarily unavailable" in response.data
    assert b"private-storage-name" not in response.data
    assert damaged.read_bytes() == b"corrupt customer storage" * 200


def test_integrity_check_does_not_create_missing_storage(tmp_path, monkeypatch):
    target = tmp_path / "missing.db"
    with monkeypatch.context() as patch:
        patch.setattr(config, "DATABASE_PATH", str(target))
        assert db.integrity_check() != "ok"
    assert not target.exists()


def test_integrity_check_returns_ok(isolated_db):
    assert db.integrity_check() == "ok"


def test_write_serialized_releases_after_failure(isolated_db):
    with pytest.raises(ValueError):
        with db.write_serialized():
            raise ValueError("operation failed")
    with db.write_serialized():
        pass
