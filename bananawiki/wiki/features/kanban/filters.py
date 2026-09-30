"""Ticket filters: the board filter bar's rules on the server.

The board page filters in the browser (``kanban-filter.js``); the board state
endpoint and "My tickets" accept the same parameters and apply the same rules
here, so a filtered address gives the same tickets everywhere:

* ``q``: every word must occur (case-insensitively) in the ticket's title,
  ``#id``, ``+label``s or ``@username``s;
* ``who``: ``me``, ``none`` (unassigned) or a user id among the assignees;
* ``label``: a label of the ticket (case-insensitive);
* ``priority``: ``low``, ``medium``, ``high`` or ``critical``;
* ``due``: ``overdue`` (before today), ``soon`` (overdue or within
  ``SOON_DAYS``), ``week`` (today to ``WEEK_DAYS`` ahead) or ``none``.

Dates are compared with today's date in the site's time zone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ....core.timeutil import utcnow
from ...templating import site_timezone
from . import fields
from .fields import KanbanError

FIELDS = ("q", "who", "label", "priority", "due")
DUE_CHOICES = ("overdue", "soon", "week", "none")
SOON_DAYS = 2  # kanban-board.js K.dueState
WEEK_DAYS = 7  # kanban-filter.js
MAX_QUERY = 200
MAX_WORDS = 20
MAX_WHO = 64


def site_today() -> date:
    """Today's date in the site's time zone (due dates are plain dates)."""
    return datetime.fromtimestamp(utcnow().timestamp(), site_timezone()).date()


@dataclass(frozen=True)
class TicketFilter:
    q: str = ""
    who: str = ""
    label: str = ""
    priority: str = ""
    due: str = ""

    @property
    def active(self) -> bool:
        return any(getattr(self, name) for name in FIELDS)

    @property
    def words(self) -> list[str]:
        return self.q.lower().split()

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def parse(args: Mapping[str, Any]) -> TicketFilter:
    """Read and validate the filter parameters (missing ones mean "any"); raises ``KanbanError``."""
    values = {name: " ".join(str(args.get(name) or "").split()) for name in FIELDS}
    if len(values["q"]) > MAX_QUERY or len(values["q"].split()) > MAX_WORDS:
        raise KanbanError("kanban.error.invalid_filter")
    if len(values["who"]) > MAX_WHO or len(values["label"]) > fields.MAX_LABEL_LENGTH:
        raise KanbanError("kanban.error.invalid_filter")
    if values["priority"] and values["priority"] not in fields.PRIORITIES:
        raise KanbanError("kanban.error.invalid_filter")
    if values["due"] and values["due"] not in DUE_CHOICES:
        raise KanbanError("kanban.error.invalid_filter")
    return TicketFilter(**values)


def _haystack(card: dict[str, Any]) -> str:
    parts = [card["title"], f"#{card['id']}"]
    parts += [f"+{label}" for label in card["labels"]]
    parts += [f"@{person['username']}" for person in card["assignees"]]
    return " ".join(parts).lower()


def _matches_due(due: str | None, choice: str, today: date) -> bool:
    if not choice:
        return True
    if choice == "none":
        return not due
    if not due:
        return False
    if choice == "overdue":
        return due < today.isoformat()
    if choice == "soon":
        return due <= (today + timedelta(days=SOON_DAYS)).isoformat()
    return today.isoformat() <= due <= (today + timedelta(days=WEEK_DAYS)).isoformat()


def matches(card: dict[str, Any], flt: TicketFilter, *, user_id: str | None, today: date) -> bool:
    """Whether a ticket card (``store.card``) passes *flt* for the user *user_id*."""
    if flt.priority and card["priority"] != flt.priority:
        return False
    if flt.label and flt.label.lower() not in {label.lower() for label in card["labels"]}:
        return False
    if flt.who:
        people = {person["id"] for person in card["assignees"]}
        if flt.who == "none":
            if people:
                return False
        elif (user_id if flt.who == "me" else flt.who) not in people:
            return False
    if not _matches_due(card.get("due_date"), flt.due, today):
        return False
    if flt.q:
        text = _haystack(card)
        return all(word in text for word in flt.words)
    return True


def apply_to_state(state: dict[str, Any], flt: TicketFilter, *, user_id: str | None,
                   today: date | None = None) -> dict[str, Any]:
    """A board state (``store.board_state``) narrowed to the matching tickets, with the counts."""
    today = today or site_today()
    total = len(state["tickets"])
    kept = {key: card for key, card in state["tickets"].items()
            if matches(card, flt, user_id=user_id, today=today)}
    return {
        **state,
        "columns": [{**column, "tickets": [tid for tid in column["tickets"] if str(tid) in kept]}
                    for column in state["columns"]],
        "tickets": kept,
        "filter": flt.as_dict(),
        "shown": len(kept),
        "total": total,
    }


def sql_conditions(flt: TicketFilter, today: date) -> tuple[list[str], list[Any]]:
    """SQL for the ``priority`` and ``due`` parts of *flt* (tickets aliased ``t``)."""
    conditions: list[str] = []
    params: list[Any] = []
    if flt.priority:
        conditions.append("t.priority = ?")
        params.append(flt.priority)
    due = "substr(t.due_date, 1, 10)"
    has_due = "COALESCE(t.due_date, '') != ''"
    if flt.due == "none":
        conditions.append(f"NOT {has_due}")
    elif flt.due == "overdue":
        conditions.append(f"{has_due} AND {due} < ?")
        params.append(today.isoformat())
    elif flt.due == "soon":
        conditions.append(f"{has_due} AND {due} <= ?")
        params.append((today + timedelta(days=SOON_DAYS)).isoformat())
    elif flt.due == "week":
        conditions.append(f"{has_due} AND {due} BETWEEN ? AND ?")
        params += [today.isoformat(), (today + timedelta(days=WEEK_DAYS)).isoformat()]
    return conditions, params
