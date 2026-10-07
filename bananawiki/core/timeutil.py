"""UTC timestamps.

BananaWiki stores timestamps as TEXT in UTC. 1.4 wrote two shapes into the
same columns: SQLite's ``YYYY-MM-DD HH:MM:SS`` and Python's ISO form
``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``. 1.6 normalises stored values to the
SQLite shape (migration 4) and always writes that shape, which sorts and
compares correctly as text and matches ``datetime('now')`` column defaults.

``datetime`` ends at the years 1 and 9999, so a value near either end cannot
always be converted to another time zone. Stored values are read as they are
(chat stores indefinite timeouts in 9999) and shown through :func:`in_zone`,
which never fails. A time someone enters is read with ``parse(..., bounded=True)``,
which accepts only the years ``MIN_YEAR`` to ``MAX_YEAR`` in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

SQL_FORMAT = "%Y-%m-%d %H:%M:%S"
MIN_YEAR = 1900
MAX_YEAR = 9998


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_sql(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        moment = moment.astimezone(UTC)
    except OverflowError:
        pass  # within a day of datetime's limits: keep the clock time rather than fail
    # isoformat() always writes four-digit years, so even such values sort as text.
    return moment.replace(tzinfo=None).isoformat(" ", "seconds")


def in_zone(moment: datetime, zone: tzinfo) -> datetime:
    """*moment* in *zone*; in UTC, or unchanged, when that would leave datetime's range."""
    for target in (zone, UTC):
        try:
            return moment.astimezone(target)
        except OverflowError:
            continue
    return moment


def now_sql() -> str:
    return to_sql(utcnow())  # type: ignore[return-value]


def sql_in(**delta: float) -> str:
    """Timestamp *delta* from now, e.g. ``sql_in(hours=48)`` or ``sql_in(days=-30)``."""
    return to_sql(utcnow() + timedelta(**delta))  # type: ignore[return-value]


def parse(value: str | datetime | float | int | None, *, bounded: bool = False) -> datetime | None:
    """Parse any timestamp shape BananaWiki has ever stored; None if invalid.

    With *bounded*, for a time from a request, a moment outside the years
    ``MIN_YEAR``-``MAX_YEAR`` in UTC is invalid too: what it accepts can be
    stored, read back and shown in any time zone.
    """
    moment = _read(value)
    if moment is None or not bounded:
        return moment
    try:
        year = moment.astimezone(UTC).year
    except OverflowError:
        return None
    return moment if MIN_YEAR <= year <= MAX_YEAR else None


def _read(value: str | datetime | float | int | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
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
