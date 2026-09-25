"""Version 3: paired-wiki sharing, separate remote copies and durable polling."""

import uuid

from sqlite_migrations import execute_script


def upgrade(connection):
    execute_script(connection, """
        CREATE TABLE IF NOT EXISTS federation_identity (
            id INTEGER PRIMARY KEY CHECK (id=1),
            wiki_id TEXT NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS federation_peers (
            wiki_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            secret TEXT NOT NULL,
            audience_category INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            generation TEXT NOT NULL,
            last_sequence INTEGER NOT NULL DEFAULT 0,
            last_success REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            next_attempt REAL NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            lease TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            last_served REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS federation_page_ids (
            page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
            public_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS federation_shares (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
            granted_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER,
            PRIMARY KEY (peer_id, page_id)
        );
        CREATE TABLE IF NOT EXISTS federation_copies (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            page_id TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_path TEXT NOT NULL,
            revision TEXT NOT NULL,
            PRIMARY KEY (peer_id, page_id)
        );
        CREATE TABLE IF NOT EXISTS federation_nonces (
            peer_id TEXT NOT NULL REFERENCES federation_peers(wiki_id) ON DELETE CASCADE,
            nonce TEXT NOT NULL,
            expires REAL NOT NULL,
            PRIMARY KEY (peer_id, nonce)
        );
    """)
    connection.execute("INSERT OR IGNORE INTO federation_identity(id, wiki_id) VALUES (1, ?)",
                       (str(uuid.uuid4()),))
