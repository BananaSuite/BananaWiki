"""When to email someone that requests are waiting for them.

Shared by the wiki and the hosting portal. Each application knows its own
queues ("sources") and recipients; this module decides, per recipient, whether
a notification is due and keeps the throttling state in two tables:

* ``<events>(id, source_id, object_id, created_at)`` - one row per submitted
  item (the ``attention.created`` event), so a new request is noticed at once
  even when another one was resolved in the meantime and the count is unchanged;
* ``<recipients>(recipient, last_event_id, counts, last_sent_at, last_daily_on,
  last_failed_at)`` - what each recipient was last told.

Modes
-----
``immediate``  one email as soon as something new arrives (one per new item at
               most: several items arriving between two runs share an email);
``digest``     like immediate, but at most one email per *interval* minutes;
               items that arrive in between are included in the next one;
``daily``      one summary a day (at *daily_hour*, local time) while anything
               is waiting; nothing else.

A recipient is only told about sources where they can act (count > 0). A
failed delivery is retried after :data:`RETRY_MINUTES`, never in a tight loop.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo

from .sqlite import Session
from .timeutil import now_sql, parse, sql_in, to_sql

MODES = ("immediate", "digest", "daily")
RETRY_MINUTES = 15
EVENT_RETENTION_DAYS = 30
POLL_SECONDS = 300
_TABLE = re.compile(r"^[a-z_]{1,64}$")


@dataclass(frozen=True)
class Policy:
    mode: str = "digest"
    interval_minutes: int = 60
    daily_hour: int = 8
    timezone: tzinfo = UTC


@dataclass
class RecipientState:
    last_event_id: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    last_sent_at: datetime | None = None
    last_daily_on: str | None = None
    last_failed_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    send: bool
    fresh: tuple[str, ...]  # sources with something new for this recipient
    advance: bool  # store the current counts and event position


def fresh_sources(state: RecipientState, counts: dict[str, int], new_event_sources: set[str]) -> tuple[str, ...]:
    """Sources where the recipient can act and something arrived since they were last told."""
    return tuple(sorted(
        source for source, count in counts.items()
        if count > 0 and (source in new_event_sources or count > state.counts.get(source, 0))
    ))


def decide(policy: Policy, state: RecipientState, counts: dict[str, int], new_event_sources: set[str],
           now: datetime) -> Decision:
    fresh = fresh_sources(state, counts, new_event_sources)
    if state.last_failed_at and now - state.last_failed_at < timedelta(minutes=RETRY_MINUTES):
        return Decision(False, fresh, False)
    if policy.mode == "daily":
        local = now.astimezone(policy.timezone)
        due = (sum(counts.values()) > 0 and state.last_daily_on != local.date().isoformat()
               and local.hour >= policy.daily_hour)
        return Decision(due, fresh, True)
    if not fresh:
        return Decision(False, fresh, True)
    if policy.mode == "digest" and state.last_sent_at is not None:
        if now - state.last_sent_at < timedelta(minutes=max(1, policy.interval_minutes)):
            return Decision(False, fresh, False)
    return Decision(True, fresh, True)


def local_day(policy: Policy, now: datetime) -> str:
    return now.astimezone(policy.timezone).date().isoformat()


# ── Storage ───────────────────────────────────────────────────────────────────


def _checked(table: str) -> str:
    if not _TABLE.match(table):
        raise ValueError(f"Invalid table name {table!r}")
    return table


def create_tables(conn: sqlite3.Connection, events_table: str, recipients_table: str) -> None:
    """Create the two state tables (idempotent)."""
    events, recipients = _checked(events_table), _checked(recipients_table)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {events} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, object_id TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{events}_created ON {events}(created_at)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {recipients} ("
        "recipient TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL DEFAULT 0, counts TEXT NOT NULL DEFAULT '{}', "
        "last_sent_at TEXT, last_daily_on TEXT, last_failed_at TEXT)"
    )


class Store:
    """Reads and writes the throttling state through a database session."""

    POLL_KEY = "*poll*"

    def __init__(self, session: Session, events_table: str, recipients_table: str):
        self.db = session
        self.events = _checked(events_table)
        self.recipients = _checked(recipients_table)

    def record_event(self, source_id: str, object_id: object = "") -> None:
        self.db.execute(f"INSERT INTO {self.events} (source_id, object_id, created_at) VALUES (?, ?, ?)",
                        (str(source_id)[:100], str(object_id if object_id is not None else "")[:100], now_sql()))

    def max_event_id(self) -> int:
        return int(self.db.scalar(f"SELECT MAX(id) FROM {self.events}", default=0) or 0)

    def sources_since(self, last_event_id: int, upto: int) -> set[str]:
        return set(self.db.column(f"SELECT DISTINCT source_id FROM {self.events} WHERE id > ? AND id <= ?",
                                  (last_event_id, upto)))

    def load(self, recipient: str) -> RecipientState:
        row = self.db.one(f"SELECT * FROM {self.recipients} WHERE recipient = ?", (recipient,))
        if row is None:
            return RecipientState()
        try:
            counts = {str(k): int(v) for k, v in json.loads(row["counts"] or "{}").items()}
        except (TypeError, ValueError, AttributeError):
            counts = {}
        return RecipientState(
            last_event_id=int(row["last_event_id"] or 0), counts=counts, last_sent_at=parse(row["last_sent_at"]),
            last_daily_on=row["last_daily_on"], last_failed_at=parse(row["last_failed_at"]),
        )

    def save(self, recipient: str, state: RecipientState) -> None:
        self.db.execute(
            f"INSERT INTO {self.recipients} (recipient, last_event_id, counts, last_sent_at, last_daily_on, "
            "last_failed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(recipient) DO UPDATE SET "
            "last_event_id = excluded.last_event_id, counts = excluded.counts, last_sent_at = excluded.last_sent_at, "
            "last_daily_on = excluded.last_daily_on, last_failed_at = excluded.last_failed_at",
            (recipient, state.last_event_id, json.dumps(state.counts, sort_keys=True),
             to_sql(state.last_sent_at) if state.last_sent_at else None, state.last_daily_on,
             to_sql(state.last_failed_at) if state.last_failed_at else None),
        )

    def forget(self, keep: set[str]) -> None:
        """Drop the state of recipients that are no longer candidates (keeps the poll marker)."""
        for recipient in self.db.column(f"SELECT recipient FROM {self.recipients}"):
            if recipient not in keep and recipient != self.POLL_KEY:
                self.db.execute(f"DELETE FROM {self.recipients} WHERE recipient = ?", (recipient,))

    def prune(self) -> int:
        """Forget events every recipient has seen once they are older than the retention period."""
        return self.db.execute(f"DELETE FROM {self.events} WHERE created_at < ?",
                               (sql_in(days=-EVENT_RETENTION_DAYS),)).rowcount

    def poll_due(self, now: datetime, *, always: bool = False) -> bool:
        """True when new events arrived or the periodic catch-up poll is due (cheap early exit)."""
        marker = self.load(self.POLL_KEY)
        if always or self.max_event_id() > marker.last_event_id:
            return True
        return marker.last_sent_at is None or (now - marker.last_sent_at).total_seconds() >= POLL_SECONDS

    def mark_polled(self, now: datetime, upto: int) -> None:
        self.save(self.POLL_KEY, RecipientState(last_event_id=upto, last_sent_at=now))


def run(store: Store, policy: Policy, recipients: list[tuple[str, dict[str, int]]], deliver, *,
        now: datetime | None = None) -> int:
    """Evaluate every recipient and call ``deliver(key, counts, fresh) -> bool`` for those due.

    *recipients* are ``(key, counts)`` pairs. Returns the number of emails sent.
    Each recipient's state is saved right after its own delivery, so a crash
    part-way through never repeats an email that was already sent.
    """
    now = now or datetime.now(UTC)
    upto = store.max_event_id()
    sent = 0
    for key, counts in recipients:
        state = store.load(key)
        new_sources = store.sources_since(state.last_event_id, upto)
        decision = decide(policy, state, counts, new_sources, now)
        if decision.send:
            if deliver(key, counts, decision.fresh):
                sent += 1
                state.last_sent_at = now
                state.last_failed_at = None
                if policy.mode == "daily":
                    state.last_daily_on = local_day(policy, now)
            else:
                state.last_failed_at = now
                store.save(key, state)
                continue
        if decision.advance:
            state.last_event_id = upto
            state.counts = {source: count for source, count in counts.items() if count}
        store.save(key, state)
    store.forget({key for key, _counts in recipients})
    store.mark_polled(now, upto)
    return sent
