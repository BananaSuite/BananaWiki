"""Schema version 24: fewer settings.

Settings few administrators ever changed are no longer offered. Most now
have one value for every wiki (``settings.FIXED``, some with an environment
override for operators); their stored values stay in ``site_settings`` and
are ignored. The ones folded into another setting are translated here so a
wiki keeps doing what it did:

* ``public_mode_show_message`` (the notice is shown whenever there is one):
  where it was off, the stored notice is emptied, so nothing appears.
* ``tts_gpu_enabled`` (a saved GPU server address is the switch): where it
  was off, the saved address is emptied (its token stays, for when an
  address is typed again).
* The chat cleanup switches, schedule and 1.4 single policy
  (``chat_cleanup_enabled``, ``chat_cleanup_frequency_days``,
  ``chat_cleanup_hour``, ``chat_{dm,group}_auto_clear_{messages,attachments}``,
  ``chat_auto_clear_*``, ``chat_message_retention_days``,
  ``chat_attachment_retention_days``) become the four numbers of days
  ``chat_{dm,group}_{message,attachment}_retention_days``: the period that
  applied before, or 0 (kept) where nothing was deleted. Cleanup now runs
  daily at 03:00 site time.
* ``bm_choices``: the per-simplification choices (``simplify:*``) are
  dropped (every simplification applies in BM mode); the features shown or
  hidden stay, as Admin → Plugins sets them.

Without a stored value to translate: ``tts_enabled_languages`` (pages are
read in the wiki's interface languages, or ``BW_TTS_LANGUAGES``),
``tts_performance_mode`` (``BW_TTS_PERFORMANCE_MODE``), ``tts_gpu_timeout``
(``BW_TTS_REMOTE_GPU_TIMEOUT``), ``chat_max_message_length`` (5000),
``profile_group_badges_enabled`` (members choose per group).

The statements are frozen here: later changes belong in later versions.
"""

from __future__ import annotations

import json
import sqlite3

from ...core.sqlite import column_names, table_exists, tuples

_SPLIT_DEFAULTS = {"auto_clear_messages": 0, "auto_clear_attachments": 1, "message_retention_days": 0,
                   "attachment_retention_days": 7}
_FIELDS = tuple(_SPLIT_DEFAULTS)


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "site_settings"):
        return
    columns = column_names(conn, "site_settings")
    rows = tuples(conn, "SELECT * FROM site_settings WHERE id = 1")
    if not rows:
        return
    names = [row[1] for row in tuples(conn, "PRAGMA table_info(site_settings)")]
    row = dict(zip(names, rows[0], strict=True))
    updates: dict[str, object] = {}
    if "public_mode_show_message" in columns and not row.get("public_mode_show_message"):
        updates["public_mode_message"] = ""
    if "tts_gpu_enabled" in columns and "tts_gpu_url" in columns and not row.get("tts_gpu_enabled"):
        updates["tts_gpu_url"] = ""
    if "chat_dm_message_retention_days" in columns:
        updates.update(_chat_retention(row))
    if "bm_choices" in columns:
        try:
            choices = json.loads(row.get("bm_choices") or "{}")
        except (TypeError, ValueError):
            choices = {}
        if isinstance(choices, dict):
            kept = {key: value for key, value in choices.items() if not str(key).startswith("simplify:")}
            updates["bm_choices"] = json.dumps(kept, sort_keys=True)
    if updates:
        assignments = ", ".join(f'"{name}" = ?' for name in updates)
        conn.execute(f"UPDATE site_settings SET {assignments} WHERE id = 1", tuple(updates.values()))


def _chat_retention(row: dict[str, object]) -> dict[str, int]:
    """The days each kind of chat content was kept under the version 23 rules (0: not deleted)."""
    split = bool(row.get("chat_cleanup_split_configured"))
    enabled = bool(row.get("chat_cleanup_enabled"))
    result: dict[str, int] = {}
    for scope in ("dm", "group"):
        resolved: dict[str, int] = {}
        for field in _FIELDS:
            specific = row.get(f"chat_{scope}_{field}")
            legacy = row.get(f"chat_{field}")
            if split or (specific is not None and specific != _SPLIT_DEFAULTS[field]):
                resolved[field] = _int(specific, _SPLIT_DEFAULTS[field])
            elif legacy is not None:
                resolved[field] = _int(legacy)
            else:
                resolved[field] = _SPLIT_DEFAULTS[field]
        for kind, switch in (("message", "auto_clear_messages"), ("attachment", "auto_clear_attachments")):
            days = resolved[f"{kind}_retention_days"]
            result[f"chat_{scope}_{kind}_retention_days"] = days if enabled and resolved[switch] and days > 0 else 0
    return result
