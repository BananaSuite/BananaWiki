"""Fixtures for the desktop launcher tests (no GUI: tkinter is never imported)."""

from __future__ import annotations

import socket

import pytest

from bananawiki.desktop.datafolder import DataFolder


@pytest.fixture
def folder(tmp_path) -> DataFolder:
    """An empty, not yet created data folder."""
    return DataFolder(tmp_path / "wiki")


@pytest.fixture
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def wiki_folder(folder) -> DataFolder:
    """A data folder with an initialised wiki database (setup not done)."""
    from bananawiki.wiki.app import open_database
    from bananawiki.wiki.config import load_config

    folder.prepare()
    environ = folder.environ(host="127.0.0.1", port=5001, language="en")
    open_database(load_config(environ, secret_key=folder.secret_key())).initialize()
    return folder


def set_setting(folder: DataFolder, sql: str, params: tuple = ()) -> None:
    import sqlite3

    conn = sqlite3.connect(folder.database)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
