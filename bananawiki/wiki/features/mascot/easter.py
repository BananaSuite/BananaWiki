"""The wiki's key-sequence easter eggs (BananaSuite ``banana-eggs/v1``) and who found them.

Administrators design the eggs under Admin -> Appearance
(``site_settings.easter_egg_configs``, cleaned by :mod:`bananawiki.core.eggs`;
never saved means the default egg, the Konami code). The mascot's "Key
sequences" switch (``konami`` in :data:`.service.EGGS`) is the master switch:
off, no page carries the eggs and their addresses answer 404.

A finished sequence is recorded once per account and egg
(``easter_egg_finds``); the first find awards the egg's badge, if it names
one, and counts for the badges' ``easter_egg`` trigger (eggs found). Finds are
trivially forged by anyone signed in, so they only ever earn cosmetic
rewards.
"""

from __future__ import annotations

from typing import Any

from ....core import eggs as shared
from ....core.timeutil import now_sql
from ... import registry, settings
from ...db import db
from . import service


def master_on() -> bool:
    """The "Key sequences" switch: off, no egg plays anywhere."""
    return service.eggs()["konami"]


def config() -> dict[str, Any]:
    """The stored eggs, cleaned (invalid ones dropped), or the default egg when nothing was ever saved."""
    raw = settings.get("easter_egg_configs")
    if raw is None:
        return shared.default_config()
    return shared.clean(raw)


def stored_text() -> str:
    """What the editor starts from: the stored list as JSON."""
    return shared.dumps(config())


def save(raw: str) -> bool:
    """Store an administrator's list; raise :class:`~bananawiki.core.eggs.EggError` on anything wrong.

    Returns whether it changed.
    """
    text = shared.dumps(shared.clean(raw, strict=True))
    if text == shared.dumps(config()):
        return False  # unchanged: the default stays "never saved"
    settings.update({"easter_egg_configs": text})
    return True


def unchanged(raw: str) -> bool:
    """Whether saving *raw* would leave the stored list as it is (a list that would be refused is a change)."""
    try:
        return shared.dumps(shared.clean(raw, strict=True)) == shared.dumps(config())
    except shared.EggError:
        return False


def playable() -> list[dict[str, Any]]:
    """The enabled eggs while the master switch is on."""
    if not master_on():
        return []
    return [egg for egg in config()["eggs"] if egg["enabled"]]


def recorded() -> list[dict[str, Any]]:
    """The eggs a find is recorded for (enabled, remembering who found them): the easter egg page lists these."""
    return [egg for egg in playable() if egg["record"]]


def egg(egg_id: str) -> dict[str, Any] | None:
    return next((item for item in recorded() if item["id"] == egg_id), None)


def finds(user_id: str) -> dict[str, str]:
    """Egg id -> when *user_id* first found it."""
    return {row["egg_id"]: row["found_at"] for row in db.all(
        "SELECT egg_id, found_at FROM easter_egg_finds WHERE user_id = ? ORDER BY found_at", (user_id,))}


def find_count(user_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM easter_egg_finds WHERE user_id = ?", (user_id,), default=0))


def record_find(user_id: str, found: dict[str, Any]) -> bool:
    """Store *user_id*'s find of *found*; False when it was already stored. A first find awards its badge."""
    new = db.execute("INSERT OR IGNORE INTO easter_egg_finds (user_id, egg_id, found_at) VALUES (?, ?, ?)",
                     (user_id, found["id"], now_sql())).rowcount > 0
    if new:
        _reward(user_id, found)
    return new


def _reward(user_id: str, found: dict[str, Any]) -> None:
    if not registry.is_enabled("badges"):
        return
    from ..badges import service as badges

    badge_id = found.get("badge")
    if badge_id and str(badge_id).isdigit():
        badge = db.one("SELECT * FROM badge_types WHERE id = ? AND enabled = 1", (int(badge_id),))
        if badge is not None:
            badges.award(user_id, badge, None, automatic=True)
    badges.evaluate(user_id)  # the "easter_egg" trigger: eggs found


def revoke(user_id: str) -> int:
    """Forget every egg *user_id* found; returns how many. Badges already awarded stay."""
    return db.execute("DELETE FROM easter_egg_finds WHERE user_id = ?", (user_id,)).rowcount


def badge_choices() -> list[dict[str, str]]:
    """The editor's badge list: every badge type, while the badges feature is on."""
    if not registry.is_enabled("badges"):
        return []
    return [{"value": str(row["id"]), "label": row["name"]}
            for row in db.all("SELECT id, name FROM badge_types ORDER BY name COLLATE NOCASE")]
