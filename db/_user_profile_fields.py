"""User-profile-fields plugin: database access.

Tables are created by the plugin's ``__init__.py`` at load time.
This module provides read/write helpers for the core route layer.
"""

from datetime import datetime, timezone
from ._connection import get_db_context, retry_on_busy


@retry_on_busy
def get_field_definitions():
    """Return all field definitions ordered by sort_order."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM user_profile_fields__definitions ORDER BY sort_order"
        ).fetchall()
    return [dict(r) for r in rows]


@retry_on_busy
def get_user_field_values(user_id):
    """Return all field values for a user as {field_key: {value, visible, label, field_type}}."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT v.*, d.key, d.label, d.field_type "
            "FROM user_profile_fields__values v "
            "JOIN user_profile_fields__definitions d ON v.field_id = d.id "
            "WHERE v.user_id=?",
            (user_id,),
        ).fetchall()
    result = {}
    for r in rows:
        result[r["key"]] = {
            "value": r["value"],
            "visible": bool(r["visible"]),
            "label": r["label"],
            "field_type": r["field_type"],
        }
    return result


@retry_on_busy
def get_visible_user_field_values(user_id):
    """Return only visible field values for a user, in definition order."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT v.value, d.key, d.label, d.field_type "
            "FROM user_profile_fields__values v "
            "JOIN user_profile_fields__definitions d ON v.field_id = d.id "
            "WHERE v.user_id=? AND v.visible=1 AND v.value!='' "
            "ORDER BY d.sort_order",
            (user_id,),
        ).fetchall()
    return [
        {
            "key": r["key"],
            "label": r["label"],
            "value": r["value"],
            "field_type": r["field_type"],
        }
        for r in rows
    ]


def save_user_field_value(user_id, field_key, value, visible):
    """Upsert a single field value for a user."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM user_profile_fields__definitions WHERE key=?",
            (field_key,),
        ).fetchone()
        if not row:
            return False
        now = datetime.now(timezone.utc).isoformat()
        existing = conn.execute(
            "SELECT id FROM user_profile_fields__values WHERE user_id=? AND field_id=?",
            (user_id, row["id"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE user_profile_fields__values SET value=?, visible=?, updated_at=? "
                "WHERE user_id=? AND field_id=?",
                (value, 1 if visible else 0, now, user_id, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO user_profile_fields__values "
                "(user_id, field_id, value, visible, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, row["id"], value, 1 if visible else 0, now),
            )
        conn.commit()
    return True


def delete_user_field_values(user_id):
    """Remove all field values for a user (e.g. on account deletion)."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM user_profile_fields__values WHERE user_id=?",
            (user_id,),
        )
        conn.commit()
