"""Outgoing webhooks: administrators register URLs that are told about wiki events.

Configuration lives in ``api_service__webhooks``; every event a webhook
subscribes to becomes a row in ``api_service__webhook_deliveries`` (the queue
and the delivery log). The ``api_service.webhooks`` job sends due deliveries:

* The body is JSON ``{"id", "event", "created_at", "data"}``; ``data`` carries
  ids, slugs, titles and names, never page content or account secrets.
* Each request is signed: ``X-BananaWiki-Signature: sha256=<hex>`` is the
  HMAC-SHA256, keyed with the webhook's secret, of ``<timestamp>.<body>``
  where ``<timestamp>`` is the ``X-BananaWiki-Timestamp`` header (Unix
  seconds). Receivers should also reject old timestamps.
* Requests go through :mod:`bananawiki.core.http`: the destination is
  resolved and checked before connecting (no private or loopback addresses
  unless the webhook allows the local network, never link-local or metadata
  addresses), redirects are not followed, time and size are bounded.
* A delivery that does not get a 2xx answer is retried with growing delays
  (:data:`BACKOFF_SECONDS`) and then marked ``failed``; a destination that is
  not allowed fails at once. Deliveries are kept for :data:`RETENTION_DAYS`.
* Deliveries queued while answering a request are attempted right after the
  response, in a short-lived background thread (at most
  :data:`PROMPT_THREADS` per process, :data:`PROMPT_BATCH` deliveries each),
  for webhooks whose last attempt succeeded. The thread claims each delivery
  exactly like the job does, so nothing is sent twice; whatever it does not
  get to (or a worker that dies meanwhile) is left to the job.
* After ``api_service_webhook_max_failures`` failed attempts in a row (site
  setting, default 20; 0 turns this off), or when nothing succeeded for
  :data:`AUTO_DISABLE_DAYS` days of failures, the webhook is switched off with
  ``disabled_reason = 'failures'`` and administrators are told through the
  attention system. Switching it on again resets the count.
* Kanban and canvas events carry ids always, and titles (and a canvas's
  slug) only for boards and canvases that are not private: a private board's
  or canvas's name stays inside the wiki. Ticket descriptions, comment bodies
  and canvas content are never sent.
* Secrets are stored encrypted with the instance key and shown once, when
  the webhook is created or its secret is rotated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from flask import after_this_request, current_app, g, has_request_context

from ....core import crypto, http
from ....core.timeutil import now_sql, sql_in
from ... import attention, settings
from ...db import connection_scope, db
from .errors import ApiError, invalid
from .serialize import iso

log = logging.getLogger("bananawiki.api_service.webhooks")

MAX_WEBHOOKS = 20
MAX_URL = 2000
MAX_DESCRIPTION = 200
BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
RETENTION_DAYS = 30
CLAIM_SECONDS = 300
RUN_BUDGET_SECONDS = 120
REQUEST_TIMEOUT = 5.0
REQUEST_TOTAL_TIMEOUT = 8.0
MAX_RESPONSE_BYTES = 64 * 1024
SCHEMES = ("https", "http")
USER_AGENT = "BananaWiki-Webhooks/1.6"
SIGNATURE_HEADER = "X-BananaWiki-Signature"
TIMESTAMP_HEADER = "X-BananaWiki-Timestamp"
PING_EVENT = "ping"
PROMPT_THREADS = 2
PROMPT_BATCH = 20
PROMPT_CONFIG = "API_SERVICE_PROMPT_WEBHOOKS"  # app.config override; default: on except in tests
MAX_FAILURES_DEFAULT = 20
MAX_FAILURES_BOUNDS = (0, 1000)
AUTO_DISABLE_DAYS = 3
DISABLED_BY_FAILURES = "failures"
ATTENTION_SOURCE = "api_service.webhooks_disabled"

Transport = Callable[..., http.HttpResponse]


# ── Event payloads (only identifiers and names) ───────────────────────────────


def _page(page: dict[str, Any] | None) -> dict[str, Any]:
    page = page or {}
    return {"id": page.get("id"), "slug": page.get("slug"), "title": page.get("title"),
            "category_id": page.get("category_id"), "revision": int(page.get("revision") or 0)}


def _category(category: dict[str, Any] | None) -> dict[str, Any]:
    category = category or {}
    return {"id": category.get("id"), "name": category.get("name"), "parent_id": category.get("parent_id")}


def _user(user: dict[str, Any] | None) -> dict[str, Any]:
    user = user or {}
    return {"id": user.get("id"), "username": user.get("username"), "role": user.get("role")}


def _named(visibility: str, **names: Any) -> dict[str, Any]:
    """Names only for what is not private; an unknown visibility counts as private."""
    shown = visibility in ("public", "shared")
    return {**{key: (value if shown else None) for key, value in names.items()}, "visibility": visibility}


def _board(board: dict[str, Any] | None) -> dict[str, Any]:
    board = board or {}
    return {"id": board.get("id"), **_named(board.get("visibility") or "public", title=board.get("title"))}


def _ticket(ticket: dict[str, Any] | None, board: dict[str, Any] | None) -> dict[str, Any]:
    ticket, board = ticket or {}, board or {}
    return {"id": ticket.get("id"), "board_id": ticket.get("board_id") or board.get("id"),
            "column_id": ticket.get("column_id"),
            "title": _named(board.get("visibility") or "public", title=ticket.get("title"))["title"]}


def _canvas(canvas: dict[str, Any] | None) -> dict[str, Any]:
    canvas = canvas or {}
    return {"id": canvas.get("id"),
            **_named(canvas.get("visibility") or "private", slug=canvas.get("slug"), title=canvas.get("title"))}


def _kanban(p: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"ticket": _ticket(p.get("ticket"), p.get("board")), "board": _board(p.get("board")),
            "actor_id": p.get("actor_id"), **extra}


def _comment(p: dict[str, Any]) -> dict[str, Any]:
    comment = p.get("comment") or {}
    return _kanban(p, comment={"id": comment.get("id"), "ticket_id": comment.get("ticket_id"),
                               "user_id": comment.get("user_id")})


EVENTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "page.created": lambda p: {"page": _page(p.get("page")), "actor_id": p.get("author_id")},
    "page.updated": lambda p: {"page": _page(p.get("page")), "actor_id": p.get("author_id"),
                               "previous_revision": int((p.get("previous") or {}).get("revision") or 0)},
    "page.moved": lambda p: {"page": _page(p.get("page")), "actor_id": p.get("actor_id"),
                             "previous_category_id": p.get("previous_category_id")},
    "page.renamed": lambda p: {"page": _page(p.get("page")), "old_slug": p.get("old_slug")},
    "page.deleted": lambda p: {"page": _page(p.get("page")), "actor_id": p.get("actor_id")},
    "page.restored": lambda p: {"page": _page(p.get("page")), "actor_id": p.get("actor_id")},
    "category.created": lambda p: {"category": _category(p.get("category")), "actor_id": p.get("actor_id")},
    "category.deleted": lambda p: {"category": _category(p.get("category")), "actor_id": p.get("actor_id")},
    "user.created": lambda p: {"user": _user(p.get("user"))},
    "user.renamed": lambda p: {"user": _user(p.get("user")), "old_username": p.get("old_username"),
                               "actor_id": p.get("changed_by")},
    "user.deleted": lambda p: {"user": _user(p.get("user")), "actor_id": p.get("deleted_by")},
    "user.role_changed": lambda p: {"user": _user(p.get("user")), "old_role": p.get("old_role"),
                                    "new_role": p.get("new_role"), "actor_id": p.get("changed_by")},
    "user.suspended": lambda p: {"user": _user(p.get("user")), "until": iso(p.get("until")),
                                 "actor_id": p.get("actor_id")},
    "kanban.board.created": lambda p: {"board": _board(p.get("board")), "actor_id": p.get("actor_id")},
    "kanban.board.updated": lambda p: {"board": _board(p.get("board")), "actor_id": p.get("actor_id")},
    "kanban.board.deleted": lambda p: {"board": _board(p.get("board")), "actor_id": p.get("actor_id")},
    "kanban.ticket.created": _kanban,
    "kanban.ticket.updated": _kanban,
    "kanban.ticket.deleted": _kanban,
    "kanban.ticket.moved": lambda p: _kanban(p, from_column_id=p.get("from_column_id"),
                                             to_column_id=p.get("to_column_id")),
    "kanban.comment.created": _comment,
    "canvas.created": lambda p: {"canvas": _canvas(p.get("canvas")), "actor_id": p.get("actor_id")},
    "canvas.updated": lambda p: {"canvas": _canvas(p.get("canvas")), "actor_id": p.get("actor_id")},
    "canvas.deleted": lambda p: {"canvas": _canvas(p.get("canvas")), "actor_id": p.get("actor_id")},
}


def event_handlers() -> dict[str, list[Callable[..., None]]]:
    """``Feature.events`` entries that queue a delivery for every subscribed webhook."""

    def handler_for(event: str) -> Callable[..., None]:
        def handle(**payload: Any) -> None:
            enqueue(event, EVENTS[event](payload))

        return handle

    return {event: [handler_for(event)] for event in EVENTS}


# ── Secrets and signatures ────────────────────────────────────────────────────


def _secret_key() -> str:
    return current_app.config["BW"].secret_key


def new_secret() -> str:
    return "whsec_" + crypto.new_token(24)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def secret_of(hook: dict[str, Any]) -> str:
    """The plain secret, or '' when it no longer decrypts (the instance key changed)."""
    return crypto.decrypt(_secret_key(), hook.get("secret"))


# ── Configuration ─────────────────────────────────────────────────────────────


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid("url", "required")
    url = value.strip()
    if len(url) > MAX_URL:
        raise invalid("url", "too_long", maximum=MAX_URL)
    try:
        http.check_url(url, schemes=SCHEMES)
    except http.BlockedDestination:
        raise invalid("url", "url") from None
    return url


def _events(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise invalid("events", "required")
    if not all(isinstance(event, str) and event in EVENTS for event in value):
        raise invalid("events", "choice", options=", ".join(EVENTS))
    return sorted(set(value))


def _description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise invalid("description", "string")
    value = " ".join(value.split())
    if len(value) > MAX_DESCRIPTION:
        raise invalid("description", "too_long", maximum=MAX_DESCRIPTION)
    return value


def get(webhook_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM api_service__webhooks WHERE id = ?", (webhook_id,))


def all_webhooks() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM api_service__webhooks ORDER BY id")


def create(*, url: Any, events: Any, description: Any = "", active: bool = True, allow_private: bool = False,
           created_by: str | None) -> tuple[dict[str, Any], str]:
    """Register a webhook; return it and its secret (shown once)."""
    values = {"url": _url(url), "events": json.dumps(_events(events)), "description": _description(description)}
    secret = new_secret()
    with db.transaction():
        if int(db.scalar("SELECT COUNT(*) FROM api_service__webhooks", default=0)) >= MAX_WEBHOOKS:
            raise ApiError(400, "webhook_limit", maximum=MAX_WEBHOOKS)
        now = now_sql()
        webhook_id = db.insert("api_service__webhooks", {
            **values, "secret": crypto.encrypt(_secret_key(), secret), "active": 1 if active else 0,
            "allow_private_network": 1 if allow_private else 0, "created_by": created_by,
            "created_at": now, "updated_at": now,
        })
    return get(webhook_id), secret  # type: ignore[return-value]


def update(hook: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply validated changes: ``url``, ``events``, ``description``, ``active``, ``allow_private_network``."""
    values: dict[str, Any] = {}
    if "url" in changes:
        values["url"] = _url(changes["url"])
    if "events" in changes:
        values["events"] = json.dumps(_events(changes["events"]))
    if "description" in changes:
        values["description"] = _description(changes["description"])
    if "active" in changes:
        # Either way an administrator has now decided: the automatic switch-off is settled.
        values.update(active=1 if changes["active"] else 0, disabled_reason=None, disabled_at=None)
        if changes["active"]:
            values.update(consecutive_failures=0, failing_since=None)
    if "allow_private_network" in changes:
        values["allow_private_network"] = 1 if changes["allow_private_network"] else 0
    if values:
        values["updated_at"] = now_sql()
        db.update("api_service__webhooks", values, "id = ?", (hook["id"],))
    return get(hook["id"])  # type: ignore[return-value]


def rotate_secret(hook: dict[str, Any]) -> str:
    secret = new_secret()
    db.update("api_service__webhooks", {"secret": crypto.encrypt(_secret_key(), secret), "updated_at": now_sql()},
              "id = ?", (hook["id"],))
    return secret


def delete(hook: dict[str, Any]) -> None:
    with db.transaction():
        db.execute("DELETE FROM api_service__webhook_deliveries WHERE webhook_id = ?", (hook["id"],))
        db.execute("DELETE FROM api_service__webhooks WHERE id = ?", (hook["id"],))


def subscribed_events(hook: dict[str, Any]) -> list[str]:
    try:
        events = json.loads(hook.get("events") or "[]")
    except ValueError:
        return []
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, str) and event in EVENTS]


def public_view(hook: dict[str, Any]) -> dict[str, Any]:
    """What the API returns about a webhook (never its secret)."""
    return {
        "id": hook["id"],
        "url": hook["url"],
        "description": hook.get("description") or "",
        "events": subscribed_events(hook),
        "active": bool(hook["active"]),
        "allow_private_network": bool(hook.get("allow_private_network")),
        "consecutive_failures": int(hook.get("consecutive_failures") or 0),
        "failing_since": iso(hook.get("failing_since")),
        "last_delivery_at": iso(hook.get("last_delivery_at")),
        "last_status": hook.get("last_status"),
        "disabled_reason": hook.get("disabled_reason"),
        "disabled_at": iso(hook.get("disabled_at")),
        "created_at": iso(hook.get("created_at")),
        "updated_at": iso(hook.get("updated_at")),
    }


# ── Queue ─────────────────────────────────────────────────────────────────────


def _queue(hook_id: int, event: str, data: dict[str, Any]) -> int:
    delivery_uuid = str(uuid.uuid4())
    now = now_sql()
    body = json.dumps({"id": delivery_uuid, "event": event, "created_at": iso(now), "data": data},
                      ensure_ascii=False, separators=(",", ":"), default=str)
    return db.insert("api_service__webhook_deliveries", {
        "webhook_id": hook_id, "delivery_uuid": delivery_uuid, "event": event, "payload": body,
        "state": "pending", "attempts": 0, "next_attempt_at": now, "created_at": now,
    })


def enqueue(event: str, data: dict[str, Any]) -> int:
    """Queue *event* for every active webhook subscribed to it; return how many deliveries were queued."""
    hooks = [hook for hook in db.all("SELECT id, events FROM api_service__webhooks WHERE active = 1")
             if event in subscribed_events(hook)]
    with db.transaction():
        queued = [_queue(hook["id"], event, data) for hook in hooks]
    _send_after_response(queued)
    return len(hooks)


def ping(hook: dict[str, Any], *, actor_id: str | None) -> int:
    """Queue a ``ping`` delivery for *hook* (whatever its subscriptions); return the delivery id."""
    delivery_id = _queue(hook["id"], PING_EVENT, {"webhook_id": hook["id"], "actor_id": actor_id})
    _send_after_response([delivery_id], even_if_failing=True)
    return delivery_id


def deliveries(hook_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
    return db.all(
        "SELECT id, webhook_id, delivery_uuid, event, state, attempts, next_attempt_at, response_status, error, "
        "duration_ms, created_at, finished_at FROM api_service__webhook_deliveries WHERE webhook_id = ? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (hook_id, limit, offset),
    )


def get_delivery(hook_id: int, delivery_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM api_service__webhook_deliveries WHERE id = ? AND webhook_id = ?",
                  (delivery_id, hook_id))


def redeliver(delivery: dict[str, Any]) -> None:
    """Send a finished delivery again (same body and delivery id), with a fresh set of attempts."""
    db.update("api_service__webhook_deliveries",
              {"state": "pending", "attempts": 0, "next_attempt_at": now_sql(), "finished_at": None, "error": None},
              "id = ?", (delivery["id"],))


def delivery_view(row: dict[str, Any], *, with_payload: bool = False) -> dict[str, Any]:
    view = {
        "id": row["id"],
        "delivery_id": row["delivery_uuid"],
        "event": row["event"],
        "state": row["state"],
        "attempts": row["attempts"],
        "next_attempt_at": iso(row.get("next_attempt_at")) if row["state"] == "pending" else None,
        "response_status": row.get("response_status"),
        "error": row.get("error"),
        "duration_ms": row.get("duration_ms"),
        "created_at": iso(row.get("created_at")),
        "finished_at": iso(row.get("finished_at")),
    }
    if with_payload:
        try:
            view["payload"] = json.loads(row["payload"])
        except ValueError:
            view["payload"] = None
    return view


# ── Sending ───────────────────────────────────────────────────────────────────


def _claim(delivery_id: int) -> bool:
    """Take a due delivery for this run; a worker that dies leaves it due again after CLAIM_SECONDS."""
    now = now_sql()
    return db.execute(
        "UPDATE api_service__webhook_deliveries SET next_attempt_at = ? "
        "WHERE id = ? AND state = 'pending' AND next_attempt_at <= ?",
        (sql_in(seconds=CLAIM_SECONDS), delivery_id, now),
    ).rowcount == 1


def _send(hook: dict[str, Any], row: dict[str, Any], transport: Transport) -> tuple[int | None, str | None, bool]:
    """One HTTP attempt: ``(status, error, final)``; *final* means no retry is useful."""
    secret = secret_of(hook)
    if not secret:
        return None, "secret_unreadable", True
    body = row["payload"].encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-BananaWiki-Event": row["event"],
        "X-BananaWiki-Delivery": row["delivery_uuid"],
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature(secret, timestamp, body),
    }
    try:
        response = transport("POST", hook["url"], headers=headers, body=body, timeout=REQUEST_TIMEOUT,
                             total_timeout=REQUEST_TOTAL_TIMEOUT, max_bytes=MAX_RESPONSE_BYTES,
                             allow_private=bool(hook.get("allow_private_network")), schemes=SCHEMES)
    except http.BlockedDestination:
        return None, "blocked_destination", True
    except http.HttpError:
        return None, "network_error", False
    if response.ok:
        return response.status, None, False
    return response.status, f"http_{response.status}", False


def max_failures() -> int:
    """Failed attempts in a row after which a webhook is switched off (0: never)."""
    try:
        value = int(settings.load().get("api_service_webhook_max_failures", MAX_FAILURES_DEFAULT))
    except (TypeError, ValueError):
        value = MAX_FAILURES_DEFAULT
    return max(MAX_FAILURES_BOUNDS[0], min(MAX_FAILURES_BOUNDS[1], value))


def _disable_if_hopeless(hook_id: int, now: str) -> bool:
    """Switch a failing webhook off (inside the transaction that recorded the failure); True when it was."""
    limit = max_failures()
    if not limit:
        return False
    return db.execute(
        "UPDATE api_service__webhooks SET active = 0, disabled_reason = ?, disabled_at = ?, updated_at = ? "
        "WHERE id = ? AND active = 1 AND (consecutive_failures >= ? OR failing_since <= ?)",
        (DISABLED_BY_FAILURES, now, now, hook_id, limit, sql_in(days=-AUTO_DISABLE_DAYS)),
    ).rowcount == 1


def disabled_count(_user: Any = None) -> int:
    """Attention count: webhooks the wiki switched off that no administrator has looked at yet."""
    return int(db.scalar("SELECT COUNT(*) FROM api_service__webhooks WHERE active = 0 AND disabled_reason IS NOT NULL",
                         default=0))


def oldest_disabled(_user: Any = None) -> str | None:
    return db.scalar("SELECT MIN(disabled_at) FROM api_service__webhooks WHERE active = 0 "
                     "AND disabled_reason IS NOT NULL")


def _record(hook: dict[str, Any], row: dict[str, Any], status: int | None, error: str | None, final: bool,
            duration_ms: int) -> bool:
    attempts = row["attempts"] + 1
    now = now_sql()
    delivered = error is None
    values: dict[str, Any] = {"attempts": attempts, "response_status": status, "error": error,
                              "duration_ms": duration_ms}
    if delivered:
        values.update(state="delivered", finished_at=now, next_attempt_at=None)
    elif final or attempts >= MAX_ATTEMPTS:
        values.update(state="failed", finished_at=now, next_attempt_at=None)
    else:
        values["next_attempt_at"] = sql_in(seconds=BACKOFF_SECONDS[attempts - 1])
    with db.transaction():
        db.update("api_service__webhook_deliveries", values, "id = ?", (row["id"],))
        db.execute(
            "UPDATE api_service__webhooks SET last_delivery_at = ?, last_status = ?, "
            "consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END, "
            "failing_since = CASE WHEN ? THEN NULL ELSE COALESCE(failing_since, ?) END WHERE id = ?",
            (now, "ok" if delivered else error, 1 if delivered else 0, 1 if delivered else 0, now, hook["id"]),
        )
        disabled = not delivered and _disable_if_hopeless(hook["id"], now)
    if disabled:
        log.warning("Webhook %s switched off after repeated failed deliveries", hook["id"])
        attention.created(ATTENTION_SOURCE, hook["id"])
    return delivered


def _attempt(row: dict[str, Any], send: Transport) -> tuple[int, bool] | None:
    """Claim and send one due delivery: ``(webhook id, delivered)``, or None when it was not ours to send."""
    hook = get(row["webhook_id"])
    if hook is None or not hook["active"] or not _claim(row["id"]):
        return None
    begun = time.monotonic()
    try:
        status, error, final = _send(hook, row, send)
    except Exception:  # noqa: BLE001 - one broken delivery must not stop the queue
        log.exception("Webhook delivery %s failed unexpectedly", row["id"])
        status, error, final = None, "internal_error", False
    return hook["id"], _record(hook, row, status, error, final, int((time.monotonic() - begun) * 1000))


def deliver_due(*, transport: Transport | None = None, budget_seconds: float = RUN_BUDGET_SECONDS) -> int:
    """Background job: send due deliveries of active webhooks; return how many were attempted.

    A webhook that fails in this run is skipped for the rest of it, so one
    unreachable receiver cannot use up the run for the others.
    """
    send = transport or http.request
    started = time.monotonic()
    failing: set[int] = set()
    attempted = 0
    while time.monotonic() - started < budget_seconds:
        rows = db.all(
            "SELECT d.* FROM api_service__webhook_deliveries d JOIN api_service__webhooks w ON w.id = d.webhook_id "
            "WHERE d.state = 'pending' AND d.next_attempt_at <= ? AND w.active = 1 "
            f"{'AND d.webhook_id NOT IN (' + ','.join('?' * len(failing)) + ')' if failing else ''} "
            "ORDER BY d.next_attempt_at, d.id LIMIT 20",
            (now_sql(), *sorted(failing)),
        )
        if not rows:
            break
        for row in rows:
            if row["webhook_id"] in failing or time.monotonic() - started >= budget_seconds:
                continue
            outcome = _attempt(row, send)
            if outcome is None:
                continue
            attempted += 1
            if not outcome[1]:
                failing.add(outcome[0])
    return attempted


# ── Prompt delivery ───────────────────────────────────────────────────────────

_prompt_slots = threading.BoundedSemaphore(PROMPT_THREADS)


def deliver_now(delivery_ids: list[int], *, even_if_failing: frozenset[int] = frozenset(),
                transport: Transport | None = None) -> int:
    """Attempt these deliveries at once if they are still due; return how many were attempted.

    Deliveries of a webhook whose last attempt failed are left to the job and
    its backoff, except the ones in *even_if_failing* (test pings).
    """
    send = transport or http.request
    attempted = 0
    failing: set[int] = set()
    for delivery_id in delivery_ids[:PROMPT_BATCH]:
        row = db.one(
            "SELECT d.*, w.consecutive_failures AS hook_failures FROM api_service__webhook_deliveries d "
            "JOIN api_service__webhooks w ON w.id = d.webhook_id "
            "WHERE d.id = ? AND d.state = 'pending' AND d.next_attempt_at <= ? AND w.active = 1",
            (delivery_id, now_sql()),
        )
        if row is None or row["webhook_id"] in failing:
            continue
        if row["hook_failures"] and delivery_id not in even_if_failing:
            continue
        outcome = _attempt(row, send)
        if outcome is not None:
            attempted += 1
            if not outcome[1]:
                failing.add(outcome[0])
    return attempted


def _prompt_enabled() -> bool:
    configured = current_app.config.get(PROMPT_CONFIG)
    if configured is not None:
        return bool(configured)
    return not current_app.config["BW"].testing


def _send_after_response(delivery_ids: list[int], *, even_if_failing: bool = False) -> None:
    """Collect deliveries queued by this request; after the response a background thread attempts them."""
    if not delivery_ids or not has_request_context() or not _prompt_enabled():
        return
    pending = g.get("api_service_webhook_prompt")
    if pending is None:
        pending = g.api_service_webhook_prompt = {"ids": [], "forced": set()}
        after_this_request(_start_prompt_thread)
    pending["ids"].extend(delivery_ids)
    if even_if_failing:
        pending["forced"].update(delivery_ids)


def _start_prompt_thread(response):
    pending = g.pop("api_service_webhook_prompt", None)
    if not pending or not _prompt_slots.acquire(blocking=False):
        return response  # busy: the delivery job sends them within a minute
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    ids, forced = list(pending["ids"]), frozenset(pending["forced"])

    def run() -> None:
        try:
            with app.app_context(), connection_scope():
                deliver_now(ids, even_if_failing=forced)
        except Exception:  # noqa: BLE001 - the job is the retry path
            log.exception("Prompt webhook delivery failed")
        finally:
            _prompt_slots.release()

    try:
        threading.Thread(target=run, name="bananawiki-webhooks", daemon=True).start()
    except RuntimeError:
        _prompt_slots.release()
    return response


def prune() -> int:
    """Drop deliveries older than RETENTION_DAYS, including ones still queued for a disabled webhook."""
    return db.execute("DELETE FROM api_service__webhook_deliveries WHERE created_at < ?",
                      (sql_in(days=-RETENTION_DAYS),)).rowcount
