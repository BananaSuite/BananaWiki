"""Who sees the mascot, and with or without sunglasses.

Every signed-in person sees it while the feature is on; visitors who are not
signed in never do. The sunglasses are a 0/1 key in the account's display
preferences (``users.accessibility``: ``mascot_shades``), so they need no table
of their own and survive "reset display settings". Only that key is read and
written here: every other saved preference stays exactly as stored, including
an old ``mascot_enabled`` key, which is ignored.

Easter eggs
-----------
Administrators switch each easter egg off or on again under Admin ->
Appearance (``site_settings.easter_eggs``, JSON of egg id -> 0/1; an egg it
does not name is on). While the sunglasses are off nobody's show, but each
account keeps its choice for when they are on again.
"""

from __future__ import annotations

import json
from typing import Any

from ... import settings
from ...db import db
from ..users import preferences

# Every easter egg, in the order the settings list them: the sunglasses after enough clicks on the mascot,
# the Konami code's shower of bananas and the mascot peeking into the "page not found" card.
EGGS = ("sunglasses", "konami", "peek")

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


def _saved_eggs() -> dict[str, Any]:
    try:
        saved = json.loads(settings.get("easter_eggs", "") or "{}")
    except ValueError:
        return {}
    return saved if isinstance(saved, dict) else {}


def _egg_on(value: Any) -> bool:
    # Only a stored 0 (or false) switches an egg off: anything unreadable leaves it as it was installed.
    return isinstance(value, str) or value is None or value != 0


def eggs() -> dict[str, bool]:
    """Which easter eggs are on, by id (see :data:`EGGS`)."""
    saved = _saved_eggs()
    return {egg: _egg_on(saved.get(egg, 1)) for egg in EGGS}


def save_eggs(chosen: dict[str, bool]) -> dict[str, bool]:
    """Switch the eggs named in *chosen* on or off; returns the ones that changed, with their new state."""
    current = eggs()
    changed = {egg: bool(on) for egg, on in chosen.items() if egg in EGGS and bool(on) != current[egg]}
    if changed:
        # Ids a later version added stay as stored, so going back and forth between versions loses nothing.
        stored = {**_saved_eggs(), **{egg: int(on) for egg, on in {**current, **changed}.items()}}
        settings.update({"easter_eggs": json.dumps(stored, sort_keys=True)})
    return changed


def state(user: dict[str, Any] | None) -> dict[str, bool]:
    """Whether *user* sees the mascot (when signed in), and with sunglasses (on every page, so one key is read).

    While the sunglasses easter egg is off nobody has them on; the account's own choice is kept.
    """
    if user is None:
        return {"enabled": False, "shades": False}
    shades = bool(_flag(preferences.stored(user), "mascot_shades")) and eggs()["sunglasses"]
    return {"enabled": True, "shades": shades}


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
