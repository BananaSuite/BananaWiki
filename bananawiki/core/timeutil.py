"""UTC timestamps.

BananaWiki stores timestamps as TEXT in UTC. 1.4 wrote two shapes into the
same columns: SQLite's ``YYYY-MM-DD HH:MM:SS`` and Python's ISO form
``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``. 1.6 normalises stored values to the
SQLite shape (migration 4) and always writes that shape, which sorts and
compares correctly as text and matches ``datetime('now')`` column defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SQL_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_sql(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(SQL_FORMAT)


def now_sql() -> str:
    return to_sql(utcnow())  # type: ignore[return-value]


def sql_in(**delta: float) -> str:
    """Timestamp *delta* from now, e.g. ``sql_in(hours=48)`` or ``sql_in(days=-30)``."""
    return to_sql(utcnow() + timedelta(**delta))  # type: ignore[return-value]


def parse(value: str | datetime | float | int | None) -> datetime | None:
    """Parse any timestamp shape BananaWiki has ever stored; None if invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def normalize(value: str | None) -> str | None:
    """Return *value* in the canonical stored shape (or unchanged if unparseable)."""
    moment = parse(value)
    return to_sql(moment) if moment else value


def is_past(value: str | datetime | None) -> bool:
    moment = parse(value)
    return moment is not None and moment <= utcnow()


def is_future(value: str | datetime | None) -> bool:
    moment = parse(value)
    return moment is not None and moment > utcnow()
