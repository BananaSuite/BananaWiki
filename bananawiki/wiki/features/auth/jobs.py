"""Background housekeeping for sign-in data."""

from __future__ import annotations

from ....core.ratelimit import SqlLimiter
from ...db import db

KEEP_SECONDS = 2 * 86400  # longer than any sign-in, sign-up or form-token window


def prune_rate_limits() -> int:
    """Forget old sign-in and sign-up counters and used form tokens."""
    return SqlLimiter(db.session).prune(KEEP_SECONDS)
