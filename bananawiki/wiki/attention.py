"""What is waiting for someone's decision: the "needs your attention" system.

Features declare their approval queues as :class:`~.registry.AttentionSource`
entries in ``Feature.attention``. This module collects them for a user
(permission-aware, one cheap count per queue, cached for the request) and is
what the account menu's red dot, the user-menu section, the admin dashboard,
the ``/attention`` page and the email notifications all read.

Queues announce new items and decisions with two events (handled by the
``attention`` feature, which records them for emails and in-app notices):

* ``attention.created`` (``source``, ``object_id``) - call :func:`created`
  after the transaction that stored a new request commits;
* ``attention.decided`` (``user_id``, ``source``, ``outcome``, ``object_id``,
  ``endpoint``) - call :func:`decided` when a request was approved or denied,
  so the person who asked is told.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from flask import g, has_request_context, url_for

from . import auth
from .registry import AttentionSource, emit, is_enabled, registry

log = logging.getLogger("bananawiki.attention")

OUTCOMES = ("approved", "denied")


def sources() -> list[AttentionSource]:
    """Every queue of an enabled feature, in display order."""
    found = []
    for feature in registry().ordered():
        if feature.attention and is_enabled(feature.id):
            found.extend(feature.attention)
    return sorted(found, key=lambda source: (source.order, source.id))


def source(source_id: str) -> AttentionSource | None:
    return next((item for item in sources() if item.id == source_id), None)


def may_see(source: AttentionSource, user: dict[str, Any] | None) -> bool:
    """Cheap pre-check: can this user's role ever act on the queue?"""
    if not user:
        return False
    return auth.is_admin(user) if source.audience == "admin" else auth.has_role("editor", user)


def _count(source: AttentionSource, user: dict[str, Any]) -> int:
    try:
        return max(0, int(source.count(user) or 0))
    except Exception:  # noqa: BLE001 - one broken queue must not break every page
        log.exception("Attention count %s failed", source.id)
        return 0


def counts(user: dict[str, Any] | None) -> dict[str, int]:
    """``source id -> count`` for every queue *user* may act on (uncached)."""
    if not user:
        return {}
    return {item.id: _count(item, user) for item in sources() if may_see(item, user)}


def _url(item: AttentionSource) -> str:
    try:
        return url_for(item.endpoint, **item.endpoint_args)
    except Exception:  # noqa: BLE001 - an endpoint of a feature that is gone
        return url_for("attention.index")


def _oldest(item: AttentionSource, user: dict[str, Any]) -> str | None:
    if item.oldest is None:
        return None
    try:
        return item.oldest(user)
    except Exception:  # noqa: BLE001
        log.exception("Attention oldest %s failed", item.id)
        return None


def items(user: dict[str, Any] | None = None, *, with_oldest: bool = False) -> list[dict[str, Any]]:
    """The non-empty queues of *user* (default: the current user), cached per request.

    Each entry has ``id``, ``label`` (translation key), ``count``, ``url`` and
    ``source``; *with_oldest* adds ``oldest`` (when the queue can tell cheaply).
    """
    user = user if user is not None else auth.current_user()
    if not user:
        return []
    cache: dict[str, list[dict[str, Any]]] | None = g.setdefault("_attention", {}) if has_request_context() else None
    entries = cache.get(user["id"]) if cache is not None else None
    if entries is None:
        entries = []
        for item in sources():
            number = _count(item, user) if may_see(item, user) else 0
            if number:
                entries.append({"id": item.id, "label": item.label, "count": number, "url": _url(item),
                                "source": item})
        if cache is not None:
            cache[user["id"]] = entries
    if not with_oldest:
        return entries
    return [{**entry, "oldest": _oldest(entry["source"], user)} for entry in entries]


def total(user: dict[str, Any] | None = None) -> int:
    return sum(entry["count"] for entry in items(user))


def badge(source_id: str) -> Callable[[Any], int]:
    """A ``NavItem.badge`` that reuses the cached count of *source_id*."""

    def count(user: Any) -> int:
        return next((entry["count"] for entry in items(user) if entry["id"] == source_id), 0)

    return count


def created(source_id: str, object_id: Any = "") -> None:
    """Announce a new item in a queue (after its transaction committed)."""
    emit("attention.created", source=source_id, object_id="" if object_id is None else str(object_id))


def decided(user_id: str | None, source_id: str, outcome: str, *, object_id: Any = "",
            endpoint: str = "") -> None:
    """Tell the person who asked that their request was approved or denied."""
    if not user_id or outcome not in OUTCOMES:
        return
    emit("attention.decided", user_id=user_id, source=source_id, outcome=outcome,
         object_id="" if object_id is None else str(object_id), endpoint=endpoint)
