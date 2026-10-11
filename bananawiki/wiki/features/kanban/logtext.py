"""Board history and activity messages that every reader sees in their own language.

Until 1.7 a message was translated into the language of the person who made
the change and stored as text, so an Italian reader saw "added the ticket …"
written by an English-speaking colleague. Messages are now stored as their
catalogue key plus values (``{"k": "kanban.log.ticket_created", "v": {...}}``)
and translated when they are shown. Rows written by older versions are plain
text and are shown unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from ...i18n import t, tn

_PREFIX = '{"k":"kanban.log.'
# Below the shortest column that stores a message (kanban_board_history.edit_message keeps 500 characters).
LIMIT = 480


def message(key: str, /, **values: Any) -> str:
    """The stored form of the catalogue message *key* with *values*.

    Long values (ticket titles, lists of names) are shortened with an ellipsis
    until the whole message fits in :data:`LIMIT` characters.
    """
    clean = {name: value if isinstance(value, (int, float)) else str(value) for name, value in values.items()}
    while True:
        text = json.dumps({"k": key, "v": clean}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(text) <= LIMIT:
            return text
        longest = max((name for name, value in clean.items() if isinstance(value, str)),
                      key=lambda name: len(clean[name]), default=None)
        if longest is None or len(clean[longest]) <= 1:
            return text[:LIMIT]
        value = clean[longest].rstrip("…")
        clean[longest] = value[: max(1, len(value) - max(8, len(text) - LIMIT + 1))] + "…"


def render(stored: str | None) -> str:
    """*stored* in the reader's language (text from older versions comes back as it is)."""
    text = stored or ""
    if not text.startswith(_PREFIX):
        return text
    try:
        data = json.loads(text)
        key = str(data["k"])
        values = data.get("v") or {}
        if not isinstance(values, dict):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        return text
    if "count" in values:
        return tn(key, values["count"], **{k: v for k, v in values.items() if k != "count"})
    return t(key, **values)
