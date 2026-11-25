"""Helpers for tests that open boards and canvases to members or change who holds which permission."""

from __future__ import annotations


def members_see(db, table: str = "kanban_boards") -> None:
    """Every board (or canvas, with ``canvas__layouts``) so far is visible to all members."""
    db.execute(f"UPDATE {table} SET visibility = 'members'")


def role_default(db, role: str, key: str, granted: bool) -> None:
    """Change one default of base *role* for this wiki (as Admin → Roles → the base role does)."""
    db.execute("INSERT OR REPLACE INTO base_role_permissions (role, permission_key, granted) VALUES (?, ?, ?)",
               (role, key, int(granted)))


def only_stored_permissions(db, user_id: str) -> None:
    """Make the keys stored for *user_id* in ``user_permissions`` their whole set.

    Since version 26 a person's own rows are changes to their role; this takes every other key away, as a
    person's own set did before.
    """
    from bananawiki.wiki import permissions

    stored = db.column("SELECT permission_key FROM user_permissions WHERE user_id = ? AND granted = 1", (user_id,))
    kept = permissions.with_implications(stored)  # as a person's own set was read: implied keys included
    db.executemany("INSERT OR IGNORE INTO user_permissions (user_id, permission_key, granted) VALUES (?, ?, 0)",
                   [(user_id, key) for key in permissions.CATALOGUE if key not in kept])
