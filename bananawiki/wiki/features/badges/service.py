"""Badge types, awards, automatic triggers and notifications.

Automatic triggers are evaluated by a background job for everyone and, for
the author only, right after they save a page or sign in - never on ordinary
page views. A badge a trigger awarded is awarded once, even when the type
allows several manual awards.
"""

from __future__ import annotations

import re
from typing import Any

from ....core.timeutil import now_sql
from ...db import db

TRIGGERS = ("first_edit", "contribution_count", "category_count", "article_count", "member_days", "reading_time")
MAX_NAME = 100
MAX_DESCRIPTION = 500
MAX_ICON = 10
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
READING_HEARTBEAT_MAX = 120

DEFAULT_BADGES = (
    ("First Edit", "Made your first contribution to the wiki", "✏️", "#4a90e2", "first_edit", 0),
    ("Prolific Contributor", "Made 10 contributions to the wiki", "🌟", "#ffd700", "contribution_count", 10),
    ("Super Contributor", "Made 50 contributions to the wiki", "💫", "#ff6b6b", "contribution_count", 50),
    ("Master Contributor", "Made 100 contributions to the wiki", "⭐", "#9b59b6", "contribution_count", 100),
    ("Diverse Contributor", "Contributed to 5 different categories", "🌈", "#3498db", "category_count", 5),
    ("Veteran", "Member for 365 days", "🎖️", "#2ecc71", "member_days", 365),
)


class BadgeError(ValueError):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Badge types ──────────────────────────────────────────────────────────────


def list_types() -> list[dict[str, Any]]:
    return db.all(
        "SELECT bt.*, (SELECT COUNT(*) FROM user_badges ub WHERE ub.badge_type_id = bt.id AND ub.revoked = 0) "
        "AS holder_count FROM badge_types bt ORDER BY bt.name COLLATE NOCASE"
    )


def get_type(type_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM badge_types WHERE id = ?", (type_id,))


def clean_type(form: Any) -> dict[str, Any]:
    """Validate the badge form (``name``, ``description``, ``icon``, ``color``, flags, trigger)."""
    name = " ".join((form.get("name") or "").split())[:MAX_NAME]
    if not name:
        raise BadgeError("badges.error.name_required")
    color = (form.get("color") or "#ffd700").strip()
    if not HEX_COLOR.fullmatch(color):
        raise BadgeError("badges.error.color_invalid")
    auto = form.get("auto_trigger") == "1"
    trigger = (form.get("trigger_type") or "").strip()
    if trigger and trigger not in TRIGGERS:
        raise BadgeError("badges.error.trigger_invalid")
    if auto and not trigger:
        raise BadgeError("badges.error.trigger_required")
    try:
        threshold = max(0, min(1_000_000, int(form.get("trigger_threshold") or 0)))
    except (TypeError, ValueError):
        raise BadgeError("badges.error.threshold_invalid") from None
    return {
        "name": name,
        "description": (form.get("description") or "").strip()[:MAX_DESCRIPTION],
        "icon": (form.get("icon") or "🏆").strip()[:MAX_ICON] or "🏆",
        "color": color.lower(),
        "enabled": 1 if form.get("enabled") == "1" else 0,
        "auto_trigger": 1 if auto else 0,
        "trigger_type": trigger,
        "trigger_threshold": threshold,
        "allow_multiple": 1 if form.get("allow_multiple") == "1" else 0,
    }


def _name_taken(name: str, except_id: int | None = None) -> bool:
    row = db.one("SELECT id FROM badge_types WHERE name = ? COLLATE NOCASE", (name,))
    return row is not None and row["id"] != except_id


def create_type(values: dict[str, Any], created_by: str | None) -> int:
    with db.transaction():
        if _name_taken(values["name"]):
            raise BadgeError("badges.error.name_taken")
        return db.insert("badge_types", {**values, "created_by": created_by, "created_at": now_sql()})


def update_type(type_id: int, values: dict[str, Any]) -> None:
    with db.transaction():
        if _name_taken(values["name"], type_id):
            raise BadgeError("badges.error.name_taken")
        db.update("badge_types", values, "id = ?", (type_id,))


def delete_type(type_id: int) -> None:
    """Delete a badge type; its awards and notifications go with it."""
    db.execute("DELETE FROM badge_types WHERE id = ?", (type_id,))


def add_defaults(created_by: str | None) -> int:
    """Create the 1.4 default badges that do not exist yet (disabled)."""
    added = 0
    with db.transaction():
        for name, description, icon, color, trigger, threshold in DEFAULT_BADGES:
            if _name_taken(name):
                continue
            db.insert("badge_types", {
                "name": name, "description": description, "icon": icon, "color": color, "enabled": 0,
                "auto_trigger": 1, "trigger_type": trigger, "trigger_threshold": threshold, "allow_multiple": 0,
                "created_by": created_by, "created_at": now_sql(),
            })
            added += 1
    return added


# ── Awards ────────────────────────────────────────────────────────────────────


def _notify(user_id: str, type_id: int) -> None:
    db.execute(
        "INSERT INTO badge_notifications (user_id, badge_type_id, notified, created_at) SELECT ?, ?, 0, ? "
        "WHERE NOT EXISTS (SELECT 1 FROM badge_notifications WHERE user_id = ? AND badge_type_id = ? AND notified = 0)",
        (user_id, type_id, now_sql(), user_id, type_id),
    )


def award(user_id: str, badge: dict[str, Any], awarded_by: str | None, *, automatic: bool = False) -> bool:
    """Award *badge*; False when the user already holds it and it may not be held twice."""
    with db.transaction():
        active = db.scalar("SELECT 1 FROM user_badges WHERE user_id = ? AND badge_type_id = ? AND revoked = 0",
                           (user_id, badge["id"]))
        if active and (automatic or not badge["allow_multiple"]):
            return False
        revoked = db.one("SELECT id FROM user_badges WHERE user_id = ? AND badge_type_id = ? AND revoked = 1 "
                         "ORDER BY id DESC LIMIT 1", (user_id, badge["id"]))
        if revoked and not active:
            db.execute("UPDATE user_badges SET revoked = 0, revoked_at = NULL, revoked_by = NULL, earned_at = ?, "
                       "awarded_by = ? WHERE id = ?", (now_sql(), awarded_by, revoked["id"]))
        else:
            db.insert("user_badges", {"user_id": user_id, "badge_type_id": badge["id"], "earned_at": now_sql(),
                                      "awarded_by": awarded_by, "revoked": 0})
        _notify(user_id, badge["id"])
    return True


def revoke(user_id: str, type_id: int, revoked_by: str | None, *, permanent: bool) -> int:
    if permanent:
        return db.execute("DELETE FROM user_badges WHERE user_id = ? AND badge_type_id = ?",
                          (user_id, type_id)).rowcount
    return db.execute(
        "UPDATE user_badges SET revoked = 1, revoked_at = ?, revoked_by = ? "
        "WHERE user_id = ? AND badge_type_id = ? AND revoked = 0",
        (now_sql(), revoked_by, user_id, type_id),
    ).rowcount


def revoke_all(type_id: int, revoked_by: str | None, *, permanent: bool) -> int:
    if permanent:
        return db.execute("DELETE FROM user_badges WHERE badge_type_id = ?", (type_id,)).rowcount
    return db.execute(
        "UPDATE user_badges SET revoked = 1, revoked_at = ?, revoked_by = ? WHERE badge_type_id = ? AND revoked = 0",
        (now_sql(), revoked_by, type_id),
    ).rowcount


def holders(type_id: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT ub.*, u.username, a.username AS awarded_by_username FROM user_badges ub "
        "JOIN users u ON u.id = ub.user_id LEFT JOIN users a ON a.id = ub.awarded_by "
        "WHERE ub.badge_type_id = ? ORDER BY ub.revoked, ub.earned_at DESC LIMIT 500",
        (type_id,),
    )


def user_badges(user_id: str) -> list[dict[str, Any]]:
    """Badges a user holds, one entry per type with the number of awards."""
    return db.all(
        "SELECT bt.id, bt.name, bt.description, bt.icon, bt.color, COUNT(*) AS times, MAX(ub.earned_at) AS earned_at "
        "FROM user_badges ub JOIN badge_types bt ON bt.id = ub.badge_type_id "
        "WHERE ub.user_id = ? AND ub.revoked = 0 AND bt.enabled = 1 GROUP BY bt.id ORDER BY earned_at DESC",
        (user_id,),
    )


# ── Notifications ────────────────────────────────────────────────────────────


def unnotified(user_id: str) -> list[dict[str, Any]]:
    return db.all(
        "SELECT bn.id, bn.created_at, bt.name, bt.description, bt.icon, bt.color FROM badge_notifications bn "
        "JOIN badge_types bt ON bt.id = bn.badge_type_id WHERE bn.user_id = ? AND bn.notified = 0 "
        "ORDER BY bn.created_at DESC",
        (user_id,),
    )


def unnotified_count(user_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM badge_notifications WHERE user_id = ? AND notified = 0",
                         (user_id,), default=0))


def dismiss(user_id: str) -> None:
    db.execute("UPDATE badge_notifications SET notified = 1 WHERE user_id = ? AND notified = 0", (user_id,))


# ── Automatic triggers ───────────────────────────────────────────────────────


def _scoped(sql: str, column: str, user_id: str | None, params: list[Any]) -> str:
    if user_id is None:
        return sql.replace("{scope}", "")
    params.append(user_id)
    return sql.replace("{scope}", f" AND {column} = ?")


_STAT_SQL = {
    "contribution_count": ("SELECT edited_by, COUNT(*) FROM page_history WHERE edited_by IS NOT NULL{scope} "
                           "GROUP BY edited_by", "edited_by"),
    "category_count": ("SELECT ph.edited_by, COUNT(DISTINCT p.category_id) FROM page_history ph "
                       "JOIN pages p ON p.id = ph.page_id WHERE ph.edited_by IS NOT NULL "
                       "AND p.category_id IS NOT NULL{scope} GROUP BY ph.edited_by", "ph.edited_by"),
    "article_count": ("SELECT h.edited_by, COUNT(*) FROM page_history h JOIN "
                      "(SELECT MIN(id) AS id FROM page_history GROUP BY page_id) f ON f.id = h.id "
                      "WHERE h.edited_by IS NOT NULL{scope} GROUP BY h.edited_by", "h.edited_by"),
    "member_days": ("SELECT id, CAST(julianday('now') - julianday(created_at) AS INTEGER) FROM users "
                    "WHERE 1{scope}", "id"),
    "reading_time": ("SELECT user_id, seconds / 60 FROM badge_reading_time WHERE 1{scope}", "user_id"),
}


def _stats(trigger: str, user_id: str | None) -> dict[str, int]:
    key = "contribution_count" if trigger == "first_edit" else trigger
    sql, column = _STAT_SQL[key]
    params: list[Any] = []
    query = _scoped(sql, column, user_id, params)
    return {row[0]: int(row[1] or 0) for row in (tuple(r.values()) for r in db.all(query, params))}


def _qualifies(trigger: str, value: int, threshold: int) -> bool:
    if trigger == "first_edit":
        return value >= 1
    return value >= max(threshold, 1)


def evaluate(user_id: str | None = None) -> int:
    """Award every enabled automatic badge its trigger now grants (one user or everyone)."""
    badges = db.all("SELECT * FROM badge_types WHERE enabled = 1 AND auto_trigger = 1 AND trigger_type != ''")
    awarded = 0
    cache: dict[str, dict[str, int]] = {}
    for badge in badges:
        trigger = badge["trigger_type"]
        if trigger not in TRIGGERS:
            continue
        stats = cache.setdefault(trigger, _stats(trigger, user_id))
        holders_now = set(db.column("SELECT user_id FROM user_badges WHERE badge_type_id = ? AND revoked = 0",
                                    (badge["id"],)))
        for candidate, value in stats.items():
            if candidate not in holders_now and _qualifies(trigger, value, badge["trigger_threshold"]):
                if award(candidate, badge, None, automatic=True):
                    awarded += 1
    return awarded


def evaluate_everyone() -> int:
    return evaluate(None)


def reading_tracked() -> bool:
    return bool(db.scalar(
        "SELECT 1 FROM badge_types WHERE enabled = 1 AND auto_trigger = 1 AND trigger_type = 'reading_time' LIMIT 1"
    ))


def add_reading_time(user_id: str, seconds: int) -> None:
    seconds = max(0, min(READING_HEARTBEAT_MAX, int(seconds)))
    if seconds:
        db.execute(
            "INSERT INTO badge_reading_time (user_id, seconds, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET seconds = seconds + excluded.seconds, updated_at = excluded.updated_at",
            (user_id, seconds, now_sql()),
        )


# ── Event handlers ────────────────────────────────────────────────────────────


def on_page_saved(page: dict[str, Any], author_id: str | None = None, **_: Any) -> None:
    if author_id:
        evaluate(author_id)


def on_login(user: dict[str, Any]) -> None:
    evaluate(user["id"])
