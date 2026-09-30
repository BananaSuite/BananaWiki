"""Validation of board, column and ticket fields, and the title shorthand.

The shorthand lets people type metadata into a ticket title, as in 1.4::

    Fix login @alice +backend !high color:red due:tomorrow

``@name`` assigns, ``+label`` labels, ``!low|medium|med|high|critical|crit``
sets the priority, ``color:`` takes a preset name or a hex colour and
``due:`` takes ``today``, ``tomorrow``, ``nextweek`` or ``YYYY-MM-DD``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ....core.timeutil import utcnow

MAX_BOARD_TITLE = 200
MAX_COLUMN_TITLE = 100
MAX_TICKET_TITLE = 300
MAX_DESCRIPTION = 10_000
MAX_COMMENT = 2_000
MAX_LABELS = 20
MAX_LABEL_LENGTH = 40
MAX_CHECKLIST_ITEMS = 100
MAX_CHECKLIST_TEXT = 300
MAX_WIP_LIMIT = 999
PRIORITIES = ("low", "medium", "high", "critical")

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PRIORITY_SHORTHAND = {"low": "low", "medium": "medium", "med": "medium", "high": "high",
                       "critical": "critical", "crit": "critical"}
COLOR_PRESETS = {"red": "#e91e63", "orange": "#ff9800", "yellow": "#ffeb3b", "green": "#4caf50",
                 "blue": "#2196f3", "purple": "#9c27b0"}


class KanbanError(ValueError):
    """A refused change; ``key`` is a translation key, ``status`` the HTTP status."""

    def __init__(self, key: str, status: int = 400, **values: Any):
        super().__init__(key)
        self.key = key
        self.status = status
        self.values = values


def title(value: Any, maximum: int, empty_key: str = "kanban.error.title_required") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise KanbanError(empty_key)
    if len(text) > maximum:
        raise KanbanError("kanban.error.title_too_long", maximum=maximum)
    return text


def description(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_DESCRIPTION:
        raise KanbanError("kanban.error.description_too_long", maximum=MAX_DESCRIPTION)
    return text


def comment(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        raise KanbanError("kanban.error.comment_required")
    if len(text) > MAX_COMMENT:
        raise KanbanError("kanban.error.comment_too_long", maximum=MAX_COMMENT)
    return text


def priority(value: Any) -> str:
    if value not in PRIORITIES:
        raise KanbanError("kanban.error.invalid_priority")
    return str(value)


def color(value: Any) -> str:
    text = str(value or "").strip()
    text = COLOR_PRESETS.get(text.lower(), text)
    if text and not _HEX_COLOR.match(text):
        raise KanbanError("kanban.error.invalid_color")
    return text.lower()


def due_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as error:
        raise KanbanError("kanban.error.invalid_date") from error


def wip_limit(value: Any) -> int | None:
    """A column's work-in-progress limit: a whole number from 1, or ``None`` (empty, 0) for no limit."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise KanbanError("kanban.error.invalid_wip_limit", maximum=MAX_WIP_LIMIT)
    try:
        number = int(str(value).strip())
    except ValueError as error:
        raise KanbanError("kanban.error.invalid_wip_limit", maximum=MAX_WIP_LIMIT) from error
    if number < 0 or number > MAX_WIP_LIMIT:
        raise KanbanError("kanban.error.invalid_wip_limit", maximum=MAX_WIP_LIMIT)
    return number or None


def stored_wip_limit(value: Any) -> int | None:
    """A limit from a snapshot or an import: invalid values mean no limit."""
    try:
        return wip_limit(value)
    except KanbanError:
        return None


def checklist_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise KanbanError("kanban.error.checklist_text_required")
    if len(text) > MAX_CHECKLIST_TEXT:
        raise KanbanError("kanban.error.checklist_text_too_long", maximum=MAX_CHECKLIST_TEXT)
    return text


def stored_checklist(value: Any) -> list[dict[str, Any]]:
    """Checklist items from a snapshot or an import: ``[{text, done}]``, invalid entries skipped."""
    result: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())[:MAX_CHECKLIST_TEXT]
        if text:
            result.append({"text": text, "done": bool(item.get("done"))})
        if len(result) >= MAX_CHECKLIST_ITEMS:
            break
    return result


def labels(value: Any) -> list[str]:
    """A de-duplicated list of valid labels from a list or a comma/space separated string."""
    if isinstance(value, str):
        parts: list[Any] = re.split(r"[\s,]+", value.strip())
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    result: list[str] = []
    seen: set[str] = set()
    for item in parts:
        label = str(item or "").strip().lstrip("+")[:MAX_LABEL_LENGTH]
        if not label or not _LABEL.match(label) or label.lower() in seen:
            continue
        seen.add(label.lower())
        result.append(label)
        if len(result) >= MAX_LABELS:
            break
    return result


def stored_labels(raw: str | None) -> list[str]:
    """Labels as stored by 1.4 and 1.6: a JSON list, or a legacy comma separated string."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = raw
    return labels(data if isinstance(data, (list, str)) else [])


def encode_labels(values: list[str]) -> str:
    return json.dumps(values) if values else ""


def _due_shorthand(value: str) -> str | None:
    today = utcnow().date()
    value = value.strip().lower()
    shortcuts = {"today": 0, "tomorrow": 1, "nextweek": 7, "next-week": 7, "next_week": 7}
    if value in shortcuts:
        return (today + timedelta(days=shortcuts[value])).isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@dataclass
class Shorthand:
    title: str
    labels: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)
    priority: str | None = None
    color: str | None = None
    due_date: str | None = None


def parse_shorthand(raw: Any) -> Shorthand:
    words: list[str] = []
    result = Shorthand(title="")
    for part in str(raw or "").split():
        lower = part.lower()
        if part.startswith("@") and _USERNAME.match(part[1:]):
            result.usernames.append(part[1:])
        elif part.startswith("+") and len(part) > 1:
            result.labels.append(part[1:])
        elif part.startswith("!") and lower[1:] in _PRIORITY_SHORTHAND:
            result.priority = _PRIORITY_SHORTHAND[lower[1:]]
        elif lower.startswith("color:") and _valid_color(part.split(":", 1)[1]):
            result.color = color(part.split(":", 1)[1])
        elif lower.startswith("due:") and _due_shorthand(part.split(":", 1)[1]):
            result.due_date = _due_shorthand(part.split(":", 1)[1])
        else:
            words.append(part)
    result.title = " ".join(words)
    result.labels = labels(result.labels)
    return result


def _valid_color(value: str) -> bool:
    try:
        return bool(color(value))
    except KanbanError:
        return False
