"""Who sees the mascot, and with or without sunglasses.

Both choices are 0/1 keys in the account's display preferences
(``users.accessibility``: ``mascot_enabled``, ``mascot_shades``), so they need
no table of their own and survive "reset display settings". Visitors who are
not signed in never see the mascot. Only these two keys are read and written
here: every other saved preference stays exactly as stored.
"""

from __future__ import annotations

import json
from typing import Any

from ...db import db
from ..users import preferences

# Clicking the mascot this many times puts its sunglasses on.
CLICKS_FOR_SHADES = 11

# Administrator actions: what each one writes on every account.
BULK_ACTIONS: dict[str, dict[str, int]] = {
    "show": {"mascot_enabled": 1},
    "hide": {"mascot_enabled": 0},
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
    """Whether *user* sees the mascot, and with sunglasses (on every page, so just the two keys are read)."""
    if user is None:
        return {"enabled": False, "shades": False}
    saved = preferences.stored(user)
    return {"enabled": bool(_flag(saved, "mascot_enabled")), "shades": bool(_flag(saved, "mascot_shades"))}


def variant(user: dict[str, Any] | None) -> str:
    """The sprite that shows *user*'s current choice (``normal``, ``shades`` or ``off``)."""
    current = state(user)
    if not current["enabled"]:
        return "off"
    return "shades" if current["shades"] else "normal"


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
