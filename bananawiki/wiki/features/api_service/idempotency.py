"""``Idempotency-Key`` for ``POST`` requests (``api_service__idempotency``).

A client that sends ``Idempotency-Key: <key>`` with a ``POST`` can safely
retry it after a timeout: the first request runs and its answer is stored;
a retry with the same key and the same request gets the stored answer back
(with ``Idempotent-Replayed: true``) instead of running again. Keys are kept
for :data:`RETENTION_HOURS`; they are unique per account and bound to the
token that first used them.

* The same key with a different method, path or body, or from another token
  of the account, is refused (422).
* Answers that carry a new secret (a token, a webhook secret: views marked
  with ``api.secret_response``) are never stored; only their status is. A
  retry of such a request is refused (409 ``idempotency_replay_unavailable``,
  with the first answer's ``status``) instead of being run again.
  Answers larger than the replay storage budget also retain their status
  and refuse replays, so a successful operation is never repeated merely
  because its answer was large.
* While the first request is still running, a retry is refused (409).
* Server errors (5xx) and rate-limit answers are not stored, so the request
  can be retried; a reservation left behind by a crashed worker expires after
  :data:`STALE_SECONDS`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ....core.timeutil import now_sql, sql_in
from ...db import db
from .errors import ApiError

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
RETENTION_HOURS = 24
STALE_SECONDS = 300
MAX_STORED_BODY = 2 * 1024 * 1024
_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")


def valid_key(value: str) -> bool:
    return bool(_KEY.match(value))


def request_hash(method: str, path: str, query: bytes, body: bytes) -> str:
    digest = hashlib.sha256()
    for part in (method.encode("ascii"), path.encode("utf-8"), query, body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def begin(user_id: str, token_id: int, key: str, fingerprint: str) -> tuple[int | None, dict[str, Any] | None]:
    """Reserve *key* for this request of *token_id*.

    Returns ``(reservation_id, None)`` when the request should run, or
    ``(None, stored)`` with ``status_code`` and ``response_body`` to replay
    (``response_body`` is None when the answer was not stored).
    """
    with db.transaction():
        row = db.one("SELECT * FROM api_service__idempotency WHERE user_id = ? AND idempotency_key = ?",
                     (user_id, key))
        if row is not None:
            expired = row["created_at"] < sql_in(hours=-RETENTION_HOURS)
            stale = row["status_code"] is None and row["created_at"] < sql_in(seconds=-STALE_SECONDS)
            if expired or stale:
                db.execute("DELETE FROM api_service__idempotency WHERE id = ?", (row["id"],))
                row = None
        if row is not None:
            if row["request_hash"] != fingerprint or row["token_id"] != token_id:
                raise ApiError(422, "idempotency_key_reused")
            if row["status_code"] is None:
                raise ApiError(409, "idempotency_in_progress")
            return None, row
        reservation = db.insert("api_service__idempotency", {
            "user_id": user_id, "token_id": token_id, "idempotency_key": key, "request_hash": fingerprint,
            "created_at": now_sql(),
        })
    return reservation, None


def finish(reservation: int, status: int, body: bytes | None) -> None:
    """Store the answer, or release the key when the answer must not be replayed.

    With *body* None (an answer carrying a secret) only the status is kept.
    """
    if status >= 500 or status == 429:
        db.execute("DELETE FROM api_service__idempotency WHERE id = ?", (reservation,))
        return
    if body is not None and len(body) > MAX_STORED_BODY:
        body = None
    db.execute("UPDATE api_service__idempotency SET status_code = ?, response_body = ? WHERE id = ?",
               (status, None if body is None else body.decode("utf-8", "replace"), reservation))


def prune() -> int:
    return db.execute("DELETE FROM api_service__idempotency WHERE created_at < ?",
                      (sql_in(hours=-RETENTION_HOURS),)).rowcount
