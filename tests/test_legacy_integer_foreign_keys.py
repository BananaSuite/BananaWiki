"""Old integer-user references migrate without losing data or constraints."""

import sqlite3

import pytest

from db._schema import _recreate_table_int_fk_to_text


def test_integer_user_reference_migration_preserves_rows_and_constraints():
    """A legacy table keeps its data, uniqueness rule, and valid user reference."""
    with sqlite3.connect(":memory:") as connection:
        connection.executescript("""
            CREATE TABLE users (id TEXT PRIMARY KEY);
            INSERT INTO users VALUES ('1');
            CREATE TABLE legacy_notes (
                id INTEGER PRIMARY KEY,
                author_id INTEGER REFERENCES users(id),
                title TEXT NOT NULL UNIQUE
            );
            INSERT INTO legacy_notes VALUES (3, 1, 'Retained note');
        """)
        _recreate_table_int_fk_to_text(connection, connection.cursor(), "legacy_notes", ["author_id"])
        columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(legacy_notes)")}
        assert columns["author_id"] == "TEXT"
        assert connection.execute("SELECT * FROM legacy_notes").fetchall() == [(3, "1", "Retained note")]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO legacy_notes VALUES (4, '1', 'Retained note')")
