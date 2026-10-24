"""Background housekeeping for sign-in data."""

from __future__ import annotations

from ....core.ratelimit import SqlLimiter
from ....core.timeutil import sql_in
from ...db import db

KEEP_SECONDS = 2 * 86400  # longer than any sign-in, sign-up or form-token window
# Ended sessions stay this long in each account's sign-in history (and for the "signed in somewhere else"
# explanation), then go: every sign-in adds a row, so the table would otherwise only grow.
SESSION_HISTORY_DAYS = 90


def prune_rate_limits() -> int:
    """Forget old sign-in and sign-up counters, used form tokens and long-ended sessions."""
    pruned = SqlLimiter(db.session).prune(KEEP_SECONDS)
    cutoff = sql_in(days=-SESSION_HISTORY_DAYS)
    pruned += db.execute(
        "DELETE FROM user_sessions WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR expires_at < ?",
        (cutoff, cutoff),
    ).rowcount
    return pruned
