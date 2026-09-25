"""Editing sessions: track who is currently editing a page."""

from ._connection import get_db_context


def heartbeat(page_id, user_id, username):
    """Update or insert an editing session heartbeat. Returns the session id."""
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO editing_sessions (page_id, user_id, username, last_heartbeat)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(page_id, user_id) DO UPDATE SET
                last_heartbeat = datetime('now'),
                username = excluded.username
            """,
            (page_id, user_id, username),
        )
        conn.commit()
        return cur.lastrowid


def get_active_editors(page_id, stale_seconds=30):
    """Return list of active editors for a page (excluding stale sessions)."""
    with get_db_context() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT user_id, username, last_heartbeat
            FROM editing_sessions
            WHERE page_id = ?
              AND datetime(last_heartbeat, '+' || ? || ' seconds') > datetime('now')
            ORDER BY last_heartbeat DESC
            """,
            (page_id, stale_seconds),
        ).fetchall()
        return [
            {"user_id": r["user_id"], "username": r["username"], "last_heartbeat": r["last_heartbeat"]}
            for r in rows
        ]


def remove_session(page_id, user_id):
    """Remove an editing session (user closed editor or navigated away)."""
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM editing_sessions WHERE page_id = ? AND user_id = ?",
            (page_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def cleanup_stale(stale_seconds=60):
    """Remove sessions older than stale_seconds (called periodically)."""
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM editing_sessions
            WHERE datetime(last_heartbeat, '+' || ? || ' seconds') < datetime('now')
            """,
            (stale_seconds,),
        )
        conn.commit()
        return cur.rowcount
