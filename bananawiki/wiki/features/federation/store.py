"""Federation state: identity, pairings, outgoing shares, received copies, nonces and poll leases.

Pairing keys are needed in clear to compute signatures, so they are stored
encrypted with the instance key (``core.crypto``) rather than hashed.
Plaintext keys from 1.4 keep working and are encrypted by the poll job.
Times in the ``federation_*`` tables are Unix seconds (REAL), as in 1.4.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any
from urllib.parse import quote

from flask import current_app

from ....core import crypto
from ... import auth
from ...db import db
from ..pages import service
from . import protocol
from .protocol import ProtocolError

LEASE_SECONDS = 120
SERVE_INTERVAL = 30
STALE_AFTER = 48 * 3600
MAX_BACKOFF = 3600


class PeerBusy(Exception):
    """The peer fetched a snapshot less than 30 seconds ago."""


def _key() -> str:
    return current_app.config["BW"].secret_key


def identity() -> str:
    row = db.one("SELECT wiki_id FROM federation_identity WHERE id = 1")
    if row is None:
        wiki = str(uuid.uuid4())
        db.execute("INSERT OR IGNORE INTO federation_identity (id, wiki_id) VALUES (1, ?)", (wiki,))
        row = db.one("SELECT wiki_id FROM federation_identity WHERE id = 1")
    return row["wiki_id"]  # type: ignore[index]


def secret_of(peer: dict[str, Any]) -> str:
    """The clear pairing key ('' when it no longer decrypts, e.g. after a key change)."""
    return crypto.decrypt(_key(), peer.get("secret"))


def encrypt_legacy_secrets() -> None:
    for row in db.all("SELECT wiki_id, secret FROM federation_peers"):
        if row["secret"] and not crypto.is_encrypted(row["secret"]):
            db.execute("UPDATE federation_peers SET secret = ? WHERE wiki_id = ?",
                       (crypto.encrypt(_key(), row["secret"]), row["wiki_id"]))


def private_network_offered() -> bool:
    """Whether a peer may be allowed on the local network: never under managed hosting (the host's network)."""
    return not current_app.config["BW"].managed_hosting


def private_network(peer: dict[str, Any]) -> bool:
    """The peer's switch as it applies now; a flag stored before the wiki became hosted is ignored."""
    return bool(peer.get("allow_private_network")) and private_network_offered()


def peers() -> list[dict[str, Any]]:
    return db.all(
        "SELECT wiki_id, name, base_url, audience_category, allow_private_network, last_sequence, last_success, "
        "last_error, next_attempt, failures FROM federation_peers ORDER BY name COLLATE NOCASE"
    )


def peer(remote_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM federation_peers WHERE wiki_id = ?", (remote_id,))


def pair(remote_id: str, name: str, origin: str, secret: str, *, category: int | None = None,
         allow_private: bool = False) -> None:
    remote_id = protocol.wiki_id(remote_id)
    origin = protocol.base_url(origin)
    secret = protocol.pairing_secret(secret)
    name = " ".join((name or "").split())
    if not name or len(name) > 100 or remote_id == identity():
        raise ProtocolError("federation.error.name")
    if allow_private and not private_network_offered():
        raise ProtocolError("federation.error.private_network_managed")
    with db.transaction():
        existing = db.all("SELECT wiki_id, base_url, secret FROM federation_peers")
        if len(existing) >= protocol.MAX_PEERS:
            raise ProtocolError("federation.error.peer_limit")
        if any(row["wiki_id"] == remote_id or row["base_url"] == origin or secret_of(row) == secret
               for row in existing):
            raise ProtocolError("federation.error.already_paired")
        if category is not None and not db.scalar("SELECT 1 FROM categories WHERE id = ?", (category,)):
            raise ProtocolError("federation.error.category")
        db.insert("federation_peers", {
            "wiki_id": remote_id, "name": name, "base_url": origin, "secret": crypto.encrypt(_key(), secret),
            "audience_category": category, "generation": secrets.token_hex(16),
            "allow_private_network": 1 if allow_private else 0,
        })


def disconnect(remote_id: str) -> None:
    """Remove a pairing; its grants, copies and nonces go with it (ON DELETE CASCADE)."""
    db.execute("DELETE FROM federation_peers WHERE wiki_id = ?", (remote_id,))


# ── Outgoing shares ───────────────────────────────────────────────────────────


def eligible(user: dict[str, Any] | None, page: dict[str, Any] | None) -> bool:
    """Only an active editor who may edit a plain Markdown page can share it."""
    if not user or not page:
        return False
    if auth.account_block(user) or user.get("force_password_change"):
        return False
    if page.get("pending_deletion") or page.get("is_deindexed") or page.get("builder_json"):
        return False
    return auth.has_role("editor", user) and service.can_edit(page, user)


def share(remote_id: str, page: dict[str, Any], user: dict[str, Any]) -> None:
    with db.transaction():
        current = service.get(page["id"])
        if not eligible(user, current):
            raise ProtocolError("federation.error.not_shareable")
        assert current is not None
        if not protocol.page_fits(current["title"], current["content"]):
            raise ProtocolError("federation.error.too_large")
        if peer(remote_id) is None:
            raise ProtocolError("federation.error.not_paired")
        if db.scalar("SELECT COUNT(*) FROM federation_shares WHERE peer_id = ?", (remote_id,), default=0) \
                >= protocol.MAX_PAGES:
            raise ProtocolError("federation.error.share_limit")
        db.execute("INSERT OR IGNORE INTO federation_page_ids (page_id, public_id) VALUES (?, ?)",
                   (current["id"], str(uuid.uuid4())))
        db.execute("INSERT OR REPLACE INTO federation_shares (peer_id, page_id, granted_by, category_id) "
                   "VALUES (?, ?, ?, ?)", (remote_id, current["id"], user["id"], current["category_id"]))


def unshare(remote_id: str, page_id: int, user: dict[str, Any]) -> bool:
    row = db.one("SELECT granted_by FROM federation_shares WHERE peer_id = ? AND page_id = ?", (remote_id, page_id))
    if row is None or (row["granted_by"] != user["id"] and not auth.is_admin(user)):
        return False
    db.execute("DELETE FROM federation_shares WHERE peer_id = ? AND page_id = ?", (remote_id, page_id))
    return True


def shares(user: dict[str, Any]) -> list[dict[str, Any]]:
    """The grants *user* may see: their own, or all of them for administrators."""
    rows = db.all(
        "SELECT s.peer_id, s.page_id, s.granted_by, p.title, p.slug, f.name FROM federation_shares s "
        "JOIN pages p ON p.id = s.page_id JOIN federation_peers f ON f.wiki_id = s.peer_id "
        "ORDER BY f.name COLLATE NOCASE, p.title COLLATE NOCASE"
    )
    return rows if auth.is_admin(user) else [row for row in rows if row["granted_by"] == user["id"]]


# ── Serving snapshots ─────────────────────────────────────────────────────────


def snapshot(headers: Any, *, now: float | None = None) -> tuple[bytes, str]:
    """Authenticate a peer's request and build its signed snapshot.

    Raises :class:`ProtocolError` (401) or :class:`PeerBusy` (429). A grant
    whose page is no longer shareable, or has moved category, is withdrawn:
    sharing again is a deliberate act.
    """
    now = time.time() if now is None else now
    with db.transaction():
        local = identity()
        remote = peer(str(headers.get("BW-Wiki", "")))
        if remote is None:
            raise ProtocolError("federation.error.authentication")
        secret = secret_of(remote)
        nonce = protocol.authenticate(headers, local, remote["wiki_id"], secret, now=now)
        db.execute("DELETE FROM federation_nonces WHERE expires < ?", (now,))
        if db.scalar("SELECT 1 FROM federation_nonces WHERE peer_id = ? AND nonce = ?", (remote["wiki_id"], nonce)):
            raise ProtocolError("federation.error.authentication")
        if remote["last_served"] > now - SERVE_INTERVAL:
            raise PeerBusy()
        db.execute("INSERT INTO federation_nonces (peer_id, nonce, expires) VALUES (?, ?, ?)",
                   (remote["wiki_id"], nonce, now + 2 * protocol.CLOCK_WINDOW))
        pages = []
        for grant in db.all("SELECT * FROM federation_shares WHERE peer_id = ?", (remote["wiki_id"],)):
            page = service.get(grant["page_id"])
            granter = db.one("SELECT * FROM users WHERE id = ?", (grant["granted_by"],))
            if (not eligible(granter, page) or page["category_id"] != grant["category_id"]  # type: ignore[index]
                    or not protocol.page_fits(page["title"], page["content"])):  # type: ignore[index]
                db.execute("DELETE FROM federation_shares WHERE peer_id = ? AND page_id = ?",
                           (remote["wiki_id"], grant["page_id"]))
                continue
            assert page is not None
            public_id = db.scalar("SELECT public_id FROM federation_page_ids WHERE page_id = ?", (page["id"],))
            path = "/page/" + quote(page["slug"], safe="")
            pages.append({"id": public_id, "title": page["title"], "content": page["content"], "source_path": path,
                          "revision": protocol.revision(page["title"], page["content"], path)})
        sequence = int(db.scalar("SELECT sequence FROM federation_identity WHERE id = 1", default=0)) + 1
        payload = protocol.validate_snapshot({"version": 1, "wiki_id": local, "sequence": sequence, "pages": pages},
                                             local)
        body = protocol.encode(payload)
        if len(body) > protocol.MAX_RESPONSE:
            raise ProtocolError("federation.error.snapshot")
        db.execute("UPDATE federation_identity SET sequence = ? WHERE id = 1", (sequence,))
        db.execute("UPDATE federation_peers SET last_served = ? WHERE wiki_id = ?", (now, remote["wiki_id"]))
    return body, protocol.response_signature(secret, local, remote["wiki_id"], nonce, body)


# ── Pulling snapshots (lease-fenced) ──────────────────────────────────────────


def claim(remote_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Take the poll lease on a peer that is due (or any idle one with *force*)."""
    now, lease = time.time(), secrets.token_hex(16)
    with db.transaction():
        row = peer(remote_id)
        if row is None or row["lease_until"] > now or (row["next_attempt"] > now and not force):
            return None
        db.execute("UPDATE federation_peers SET lease = ?, lease_until = ? WHERE wiki_id = ?",
                   (lease, now + LEASE_SECONDS, remote_id))
    return {**row, "lease": lease}


def finish(claimed: dict[str, Any], payload: dict[str, Any] | None, error_key: str = "") -> bool:
    """Store a whole snapshot, or the failure and next retry, if this worker still holds the lease."""
    now = time.time()
    with db.transaction():
        current = db.one(
            "SELECT * FROM federation_peers WHERE wiki_id = ? AND generation = ? AND lease = ? AND lease_until > ?",
            (claimed["wiki_id"], claimed["generation"], claimed["lease"], now),
        )
        if current is None:
            return False
        if payload is None or payload["sequence"] <= current["last_sequence"]:
            failures = min(current["failures"] + 1, 10)
            db.execute(
                "UPDATE federation_peers SET failures = ?, last_error = ?, next_attempt = ?, lease = '', "
                "lease_until = 0 WHERE wiki_id = ?",
                (failures, error_key or "federation.error.sync_failed",
                 now + min(MAX_BACKOFF, 30 * 2 ** failures), current["wiki_id"]),
            )
            return False
        db.execute("DELETE FROM federation_copies WHERE peer_id = ?", (current["wiki_id"],))
        db.executemany(
            "INSERT INTO federation_copies (peer_id, page_id, title, content, source_path, revision) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(current["wiki_id"], page["id"], page["title"], page["content"], page["source_path"], page["revision"])
             for page in payload["pages"]],
        )
        db.execute(
            "UPDATE federation_peers SET last_sequence = ?, last_success = ?, last_error = '', failures = 0, "
            "next_attempt = ?, lease = '', lease_until = 0 WHERE wiki_id = ?",
            (payload["sequence"], now, now + protocol.POLL_SECONDS, current["wiki_id"]),
        )
    return True


# ── Received copies ───────────────────────────────────────────────────────────


def can_read(user: dict[str, Any] | None, copy: dict[str, Any] | None) -> bool:
    """Administrators, or readers of the pairing's audience category who hold ``page.view_all``.

    Copies are pages too, so the permission that gates local pages gates them.
    Stale copies are hidden.
    """
    if not user or not copy or copy["last_success"] < time.time() - STALE_AFTER:
        return False
    if auth.is_admin(user):
        return True
    if not auth.has_permission("page.view_all", user):
        return False
    return copy["audience_category"] is not None and auth.can_read_category(copy["audience_category"], user)


def copies() -> list[dict[str, Any]]:
    return db.all(
        "SELECT c.peer_id, c.page_id, c.title, p.name, p.audience_category, p.last_success, p.last_error "
        "FROM federation_copies c JOIN federation_peers p ON p.wiki_id = c.peer_id "
        "ORDER BY p.name COLLATE NOCASE, c.title COLLATE NOCASE"
    )


def copy(remote_id: str, page_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT c.*, p.name, p.base_url, p.audience_category, p.last_success, p.last_error "
        "FROM federation_copies c JOIN federation_peers p ON p.wiki_id = c.peer_id "
        "WHERE c.peer_id = ? AND c.page_id = ?", (remote_id, page_id),
    )


def fork(item: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Turn a received copy into an ordinary local page (history, search and events included)."""
    if item["audience_category"] is None:
        raise ProtocolError("federation.error.fork_needs_category")
    content = (f"Source: {item['base_url']}{item['source_path']}\nSource wiki: {item['peer_id']}\n"
               f"Revision: {item['revision']}\n\n{item['content']}")
    return service.create(item["title"], content, category_id=item["audience_category"], author_id=user["id"],
                          slug="federated-" + secrets.token_hex(12),
                          edit_message=f"Forked from {item['name']} ({item['peer_id']})")
