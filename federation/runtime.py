"""Bounded periodic pull jobs; retry deadlines and worker leases live in SQLite."""

import logging
import threading
import time

import config
import db
from . import protocol, store

_lock = threading.Lock()
_started = False
logger = logging.getLogger(__name__)


def sync_peer(remote_id):
    settings = db.get_site_settings()
    if (not config.FEDERATION_ENABLED or not settings or not settings["setup_done"]
            or settings["maintenance_mode"] or settings.get("banana_mode")):
        return False
    if config.MANAGED_HOSTING:
        from helpers._storage_quota import mutation_would_exceed_quota
        if mutation_would_exceed_quota(protocol.MAX_RESPONSE * 2)[0]:
            return False
    peer = store.claim(remote_id)
    if not peer:
        return False
    try:
        payload = protocol.fetch(store.identity(), peer)
        # Operator disable during an in-flight request must not accept new data.
        if not config.FEDERATION_ENABLED:
            return store.finish(peer)
        return store.finish(peer, payload)
    except Exception:
        # Do not log credentials, remote response bodies, or shared content.
        logger.warning("Federation synchronization failed for peer %s", remote_id)
        store.finish(peer)
        return False


def sync_due():
    if config.FEDERATION_ENABLED:
        for peer in store.peers():
            if peer["next_attempt"] <= time.time():
                sync_peer(peer["wiki_id"])


def start():
    global _started
    if not config.FEDERATION_ENABLED:
        return
    with _lock:
        if _started:
            return
        _started = True

    def run():
        while True:
            time.sleep(30)
            try:
                sync_due()
            except Exception:
                logger.exception("Federation polling failed")

    threading.Thread(target=run, name="bw-federation", daemon=True).start()
