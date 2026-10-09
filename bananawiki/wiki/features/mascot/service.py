"""Who sees the mascot, and with or without sunglasses.

Both choices are 0/1 keys in the account's display preferences
(``users.accessibility``: ``mascot_enabled``, ``mascot_shades``), so they need
no table of their own and survive "reset display settings". Visitors who are
not signed in never see the mascot.
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


def state(user: dict[str, Any] | None) -> dict[str, bool]:
    if user is None:
        return {"enabled": False, "shades": False}
    prefs = preferences.current(user)
    return {"enabled": bool(prefs["mascot_enabled"]), "shades": bool(prefs["mascot_shades"])}


def variant(user: dict[str, Any] | None) -> str:
    """The sprite that shows *user*'s current choice (``normal``, ``shades`` or ``off``)."""
    current = state(user)
    if not current["enabled"]:
        return "off"
    return "shades" if current["shades"] else "normal"


def update(user: dict[str, Any], **values: int) -> None:
    """Set mascot keys on one account, keeping every other preference as saved."""
    row = db.one("SELECT accessibility FROM users WHERE id = ?", (user["id"],))
    current = preferences.parse(row["accessibility"] if row else None)
    prefs = preferences.clean(values, current)
    db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps(prefs), user["id"]))


def apply_to_everyone(action: str) -> int:
    """Run one of :data:`BULK_ACTIONS` on every account; returns how many changed."""
    values = BULK_ACTIONS[action]
    changed = 0
    with db.transaction():
        for row in db.all("SELECT id, accessibility FROM users"):
            current = preferences.parse(row["accessibility"])
            prefs = preferences.clean(values, current)
            if all(preferences.clean({}, current)[key] == value for key, value in values.items()):
                continue
            db.execute("UPDATE users SET accessibility = ? WHERE id = ?", (json.dumps(prefs), row["id"]))
            changed += 1
    return changed
