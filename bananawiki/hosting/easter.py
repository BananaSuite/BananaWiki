"""The portal's key-sequence easter eggs (BananaSuite ``banana-eggs/v1``) and who found them.

Administrators design them under Settings -> Easter eggs
(``hosting_settings.easter_egg_configs``); the "Konami code" switch there
(``easter_konami``) is the switch for all of them. Signed-in accounts' finds
are kept in ``account_egg_finds``; visitors keep theirs in their browser
(banana-eggs.js). The portal has no badges: finds are only listed.
"""

from __future__ import annotations

from typing import Any

from ..core import eggs as shared
from ..core.timeutil import now_sql
from . import settings
from .db import db


def master_on() -> bool:
    return settings.easter_eggs()["konami"]


def playable() -> list[dict[str, Any]]:
    """The enabled eggs while the switch is on."""
    if not master_on():
        return []
    return [egg for egg in settings.egg_config()["eggs"] if egg["enabled"]]


def recorded() -> list[dict[str, Any]]:
    """The eggs whose finds are kept: the easter egg page lists these."""
    return [egg for egg in playable() if egg["record"]]


def egg(egg_id: str) -> dict[str, Any] | None:
    return next((item for item in recorded() if item["id"] == egg_id), None)


def finds(account_id: str) -> dict[str, str]:
    return {row["egg_id"]: row["found_at"] for row in db.all(
        "SELECT egg_id, found_at FROM account_egg_finds WHERE account_id = ? ORDER BY found_at", (account_id,))}


def record_find(account_id: str, found: dict[str, Any]) -> bool:
    """Store the find; False when it was already stored."""
    return db.execute("INSERT OR IGNORE INTO account_egg_finds (account_id, egg_id, found_at) VALUES (?, ?, ?)",
                      (account_id, found["id"], now_sql())).rowcount > 0


def save(raw: str) -> bool:
    """Store an administrator's list; raise :class:`~bananawiki.core.eggs.EggError` on anything wrong.

    The page's Save always sends the list: an unchanged one is not written (the default stays "never saved"
    and the settings log names no change). Returns whether it changed.
    """
    text = shared.dumps(shared.clean(raw, strict=True))
    if text == shared.dumps(settings.egg_config()):
        return False
    settings.update(easter_egg_configs=text)
    return True
