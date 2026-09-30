"""Pulling snapshots from paired wikis: one peer on demand, or every due peer from the poll job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from flask import current_app

from ....core import http
from ... import settings, storage
from ...db import db
from . import protocol, store
from .protocol import ProtocolError

log = logging.getLogger("bananawiki.federation")

Transport = Callable[..., http.HttpResponse]


def active() -> bool:
    return bool(current_app.config["BW"].federation_enabled)


def _may_sync() -> bool:
    if not active() or not settings.setup_done() or settings.maintenance_active():
        return False
    return storage.quota_allows(protocol.MAX_RESPONSE * 2)


def fetch(local_id: str, peer: dict[str, Any], *, transport: Transport | None = None) -> dict[str, Any]:
    """Fetch and verify one peer's snapshot. Raises ProtocolError or http.HttpError."""
    secret = store.secret_of(peer)
    if not secret:
        raise ProtocolError("federation.error.secret_unreadable")
    headers = protocol.request_headers(local_id, peer["wiki_id"], secret)
    response = (transport or http.request)(
        "GET", protocol.base_url(peer["base_url"]) + protocol.PATH, headers=headers,
        timeout=10, total_timeout=25, max_bytes=protocol.MAX_RESPONSE,
        allow_private=bool(peer.get("allow_private_network")),
    )
    if response.status != 200:
        raise ProtocolError("federation.error.remote_status")
    protocol.verify_response(secret, peer["wiki_id"], local_id, headers["BW-Nonce"], response.body,
                             response.header("BW-Signature"))
    try:
        payload = json.loads(response.body)
    except ValueError:
        raise ProtocolError("federation.error.snapshot") from None
    return protocol.validate_snapshot(payload, peer["wiki_id"])


def sync_peer(remote_id: str, *, force: bool = False, transport: Transport | None = None) -> bool:
    """Synchronise one peer if it is due (or now, with *force*); True when a snapshot was stored."""
    if not _may_sync():
        return False
    claimed = store.claim(remote_id, force=force)
    if claimed is None:
        return False
    try:
        payload = fetch(store.identity(), claimed, transport=transport)
    except ProtocolError as error:
        return store.finish(claimed, None, error.key)
    except http.BlockedDestination:
        return store.finish(claimed, None, "federation.error.blocked_address")
    except http.HttpError:
        return store.finish(claimed, None, "federation.error.network")
    except Exception:  # noqa: BLE001 - never log credentials or remote content
        log.warning("Federation synchronisation with peer %s failed", remote_id)
        return store.finish(claimed, None, "federation.error.sync_failed")
    if not active():
        return store.finish(claimed, None)
    return store.finish(claimed, payload)


def poll() -> None:
    """Job: synchronise every peer whose next attempt is due."""
    if not active():
        return
    store.encrypt_legacy_secrets()
    now = time.time()
    for row in db.all("SELECT wiki_id, next_attempt FROM federation_peers"):
        if row["next_attempt"] <= now:
            sync_peer(row["wiki_id"])
