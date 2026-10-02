"""Rate limiting.

Two limiters with different trade-offs:

* :class:`MemoryLimiter` - sliding window counters in process memory, bounded
  in size. Used for the cheap global request limit and chatty endpoints where
  a per-worker approximation is fine and a database write per request is not.
* :class:`SqlLimiter` - counters in the ``rate_limit_hits`` table, shared by
  every worker. Used where the limit is a security control (sign-in, sign-up,
  password reset), so running several workers does not multiply the budget.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

from .sqlite import Session
from .timeutil import sql_in


class MemoryLimiter:
    def __init__(self, max_keys: int = 20_000):
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window: float) -> bool:
        """Record a hit; return False when *key* already used *limit* in *window* seconds."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
                if len(self._hits) > self._max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class SqlLimiter:
    """Counters stored in ``rate_limit_hits(ip, bucket, hit_at)``."""

    TABLE = "rate_limit_hits"

    def __init__(self, session: Session):
        self.db = session

    def count(self, key: str, bucket: str, window_seconds: int) -> int:
        return int(self.db.scalar(
            f"SELECT COUNT(*) FROM {self.TABLE} WHERE ip = ? AND bucket = ? AND hit_at > ?",
            (key, bucket, sql_in(seconds=-window_seconds)),
            default=0,
        ))

    def exceeded(self, key: str, bucket: str, limit: int, window_seconds: int) -> bool:
        return self.count(key, bucket, window_seconds) >= limit

    def record(self, key: str, bucket: str) -> int:
        cursor = self.db.execute(
            f"INSERT INTO {self.TABLE} (ip, bucket, hit_at) VALUES (?, ?, ?)",
            (key, bucket, sql_in(seconds=0)),
        )
        return int(cursor.lastrowid)

    def reserve(self, key: str, bucket: str, limit: int, window_seconds: int) -> int | None:
        """Atomically reserve a hit before an expensive operation; return its row id.

        Call :meth:`release` after a successful credential check when only
        failed attempts should consume the budget. Unfinished checks retain
        their hit, so concurrent requests cannot all pass the same counter.
        """
        with self.db.transaction():
            if self.exceeded(key, bucket, limit, window_seconds):
                return None
            return self.record(key, bucket)

    def release(self, reservation_id: int) -> None:
        """Release this attempt only, preserving other requests' counters."""
        self.db.execute(f"DELETE FROM {self.TABLE} WHERE id = ?", (reservation_id,))

    def hit(self, key: str, bucket: str, limit: int, window_seconds: int) -> bool:
        """Record a hit unless over the limit; return whether it was allowed."""
        return self.reserve(key, bucket, limit, window_seconds) is not None

    def clear(self, key: str, bucket: str) -> None:
        self.db.execute(f"DELETE FROM {self.TABLE} WHERE ip = ? AND bucket = ?", (key, bucket))

    def prune(self, older_than_seconds: int = 86400) -> int:
        cursor = self.db.execute(
            f"DELETE FROM {self.TABLE} WHERE hit_at < ?", (sql_in(seconds=-older_than_seconds),)
        )
        return cursor.rowcount
