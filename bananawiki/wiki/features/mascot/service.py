"""Who sees the mascot, and with or without sunglasses.

Every signed-in person sees it while the feature is on; visitors who are not
signed in never do. The sunglasses are a 0/1 key in the account's display
preferences (``users.accessibility``: ``mascot_shades``), so they need no table
of their own and survive "reset display settings". Only that key is read and
written here: every other saved preference stays exactly as stored, including
an old ``mascot_enabled`` key, which is ignored.
"""

from __future__ import annotations

import json
from typing import Any

from ...db import db
from ..users import preferences

# Administrator actions: what each one writes on every account.
BULK_ACTIONS: dict[str, dict[str, int]] = {
    "shades_on": {"mascot_shades": 1},
    "shades_off": {"mascot_shades": 0},
}


def _flag(saved: dict[str, Any], key: str) -> int:
    """*key* of the saved preferences as 0/1, with the default :func:`preferences.clean` would use."""
    default = preferences.DEFAULTS[key]
    try:
        value = int(float(saved.get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default
    return value if value in (0, 1) else default


def state(user: dict[str, Any] | None) -> dict[str, bool]:
    """Whether *user* sees the mascot (when signed in), and with sunglasses (on every page, so one key is read)."""
    if user is None:
        return {"enabled": False, "shades": False}
    return {"enabled": True, "shades": bool(_flag(preferences.stored(user), "mascot_shades"))}


def update(user: dict[str, Any], **values: int) -> None:
    """Set mascot keys on one account, keeping every other preference as saved."""
    with db.transaction():
        raw = db.scalar("SELECT accessibility FROM users WHERE id = ?", (user["id"],))
        db.execute("UPDATE users SET accessibility = ? WHERE id = ?",
                   (json.dumps({**preferences.parse(raw), **values}), user["id"]))


def apply_to_everyone(action: str) -> int:
    """Run one of :data:`BULK_ACTIONS` on every account; returns how many changed.

    Accounts that already have that choice are left alone. The others are
    written in one batch, so the write lock is held briefly however many
    accounts there are.
    """
    values = BULK_ACTIONS[action]
    with db.transaction():
        rows = []
        for row in db.all("SELECT id, accessibility FROM users"):
            saved = preferences.parse(row["accessibility"])
            if any(_flag(saved, key) != value for key, value in values.items()):
                rows.append((json.dumps({**saved, **values}), row["id"]))
        db.executemany("UPDATE users SET accessibility = ? WHERE id = ?", rows)
    return len(rows)
