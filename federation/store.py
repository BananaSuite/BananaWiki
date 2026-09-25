"""Transactional pairing, explicit sharing, and lease-fenced remote snapshots."""

import secrets
import time
import uuid
from urllib.parse import quote

import db
from helpers import editor_has_category_access, user_can_view_page

from . import protocol


def identity():
    with db.get_db_context() as conn:
        return conn.execute("SELECT wiki_id FROM federation_identity WHERE id=1").fetchone()[0]


def peers():
    with db.get_db_context() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT wiki_id, name, base_url, audience_category, last_sequence, last_success, "
            "last_error, next_attempt, failures FROM federation_peers ORDER BY name")]


def pair(remote_id, name, origin, secret, category=None):
    remote_id, origin, secret = protocol.wiki_id(remote_id), protocol.base_url(origin), protocol.pairing_secret(secret)
    if not name.strip() or len(name) > 100 or remote_id == identity():
        raise ValueError("Choose a name and a different wiki identity.")
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT count(*) FROM federation_peers").fetchone()[0] >= protocol.MAX_PEERS:
            raise ValueError("The pairing limit has been reached.")
        if conn.execute("SELECT 1 FROM federation_peers WHERE wiki_id=? OR base_url=? OR secret=?",
                        (remote_id, origin, secret)).fetchone():
            raise ValueError("That peer or key is already paired. Disconnect before pairing again.")
        if category is not None and not conn.execute("SELECT 1 FROM categories WHERE id=?", (category,)).fetchone():
            raise ValueError("The audience category does not exist.")
        conn.execute("INSERT INTO federation_peers(wiki_id,name,base_url,secret,audience_category,generation) "
                     "VALUES (?,?,?,?,?,?)", (remote_id, name.strip(), origin, secret, category, secrets.token_hex(16)))
        conn.commit()


def disconnect(remote_id):
    with db.get_db_context() as conn:
        conn.execute("DELETE FROM federation_peers WHERE wiki_id=?", (remote_id,))
        conn.commit()


def _eligible(user, page):
    return (user and page and not user["suspended"]
            and user.get("approval_status") not in ("pending", "denied")
            and not user.get("force_password_change")
            and user["role"] in ("editor", "admin", "owner")
            and not page["pending_deletion"] and not page["is_deindexed"]
            and not page.get("builder_json")
            and user_can_view_page(user, page)
            and editor_has_category_access(user, page["category_id"])
            and db.has_permission(user, "page.edit"))


def share(remote_id, page_id, user_id):
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        page = conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not _eligible(user, page):
            raise ValueError("Share only an active Markdown page you are authorized to edit.")
        if len(page["content"].encode()) > protocol.MAX_CONTENT or not 1 <= len(page["title"]) <= 300:
            raise ValueError("Shared pages are limited to 128 KiB and titles to 300 characters.")
        if not conn.execute("SELECT 1 FROM federation_peers WHERE wiki_id=?", (remote_id,)).fetchone():
            raise ValueError("The wiki is not paired.")
        if conn.execute("SELECT count(*) FROM federation_shares WHERE peer_id=?", (remote_id,)).fetchone()[0] >= protocol.MAX_PAGES:
            raise ValueError("This peer's shared-page limit has been reached.")
        conn.execute("INSERT OR IGNORE INTO federation_page_ids(page_id,public_id) VALUES (?,?)",
                     (page_id, str(uuid.uuid4())))
        conn.execute("INSERT OR REPLACE INTO federation_shares(peer_id,page_id,granted_by,category_id) VALUES (?,?,?,?)",
                     (remote_id, page_id, user_id, page["category_id"]))
        conn.commit()


def unshare(remote_id, page_id, user):
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT granted_by FROM federation_shares WHERE peer_id=? AND page_id=?",
                           (remote_id, page_id)).fetchone()
        if row and (row[0] == user["id"] or user["role"] in ("admin", "owner")):
            conn.execute("DELETE FROM federation_shares WHERE peer_id=? AND page_id=?", (remote_id, page_id))
        conn.commit()


def shares():
    with db.get_db_context() as conn:
        return conn.execute("SELECT s.*, p.title, p.slug, f.name FROM federation_shares s "
                            "JOIN pages p ON p.id=s.page_id JOIN federation_peers f ON f.wiki_id=s.peer_id "
                            "ORDER BY f.name, p.title").fetchall()


class AuthenticationError(ValueError):
    pass


class PeerBusy(ValueError):
    pass


def snapshot(headers):
    now = time.time()
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        local = conn.execute("SELECT * FROM federation_identity WHERE id=1").fetchone()
        peer = conn.execute("SELECT * FROM federation_peers WHERE wiki_id=?", (headers.get("BW-Wiki", ""),)).fetchone()
        try:
            if not peer:
                raise ValueError("Unknown peer")
            nonce = protocol.authenticate(headers, local["wiki_id"], peer, now=now)
        except ValueError as error:
            raise AuthenticationError("Invalid federation authentication.") from error
        conn.execute("DELETE FROM federation_nonces WHERE expires<?", (now,))
        if conn.execute("SELECT 1 FROM federation_nonces WHERE peer_id=? AND nonce=?", (peer["wiki_id"], nonce)).fetchone():
            raise AuthenticationError("Invalid federation authentication.")
        if peer["last_served"] > now - 30:
            raise PeerBusy("Retry in 30 seconds.")
        conn.execute("INSERT INTO federation_nonces VALUES (?,?,?)", (peer["wiki_id"], nonce, now + 2 * protocol.CLOCK_WINDOW))
        pages = []
        grants = conn.execute("SELECT * FROM federation_shares WHERE peer_id=?", (peer["wiki_id"],)).fetchall()
        for grant in grants:
            page = conn.execute("SELECT * FROM pages WHERE id=?", (grant["page_id"],)).fetchone()
            user = conn.execute("SELECT * FROM users WHERE id=?", (grant["granted_by"],)).fetchone()
            if (not _eligible(user, page) or page["category_id"] != grant["category_id"]
                    or len(page["content"].encode()) > protocol.MAX_CONTENT
                    or not 1 <= len(page["title"]) <= 300):
                # Permission/category changes require a deliberate new share.
                conn.execute("DELETE FROM federation_shares WHERE peer_id=? AND page_id=?",
                             (peer["wiki_id"], grant["page_id"]))
                continue
            public_id = conn.execute("SELECT public_id FROM federation_page_ids WHERE page_id=?", (page["id"],)).fetchone()[0]
            path = "/page/" + quote(page["slug"], safe="")
            pages.append({"id": public_id, "title": page["title"], "content": page["content"],
                          "source_path": path, "revision": protocol.revision(page["title"], page["content"], path)})
        payload = {"version": 1, "wiki_id": local["wiki_id"], "sequence": local["sequence"] + 1, "pages": pages}
        protocol.validate_snapshot(payload, local["wiki_id"])
        body = protocol.encode(payload)
        if len(body) > protocol.MAX_RESPONSE:
            raise ValueError("Snapshot size limit exceeded.")
        signed = protocol.response_signature(peer["secret"], local["wiki_id"], peer["wiki_id"], nonce, body)
        conn.execute("UPDATE federation_identity SET sequence=sequence+1 WHERE id=1")
        conn.execute("UPDATE federation_peers SET last_served=? WHERE wiki_id=?", (now, peer["wiki_id"]))
        conn.commit()
        return body, signed


def claim(remote_id):
    now, lease = time.time(), secrets.token_hex(16)
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        peer = conn.execute("SELECT * FROM federation_peers WHERE wiki_id=?", (remote_id,)).fetchone()
        if not peer or peer["lease_until"] > now or peer["next_attempt"] > now:
            return None
        conn.execute("UPDATE federation_peers SET lease=?, lease_until=? WHERE wiki_id=?", (lease, now + 120, remote_id))
        conn.commit()
        return dict(peer, lease=lease)


def finish(peer, payload=None):
    """Commit a whole snapshot or retry state only while this worker owns its lease.

    Returns True on success, False if the lease was lost or the snapshot is
    stale. Callers should re-claim the lease when False is returned and the
    lease has expired (None returned when lease expired).
    """
    now = time.time()
    if payload is not None:
        protocol.validate_snapshot(payload, peer["wiki_id"])
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM federation_peers WHERE wiki_id=? AND generation=? AND lease=? AND lease_until>?",
                                (peer["wiki_id"], peer["generation"], peer["lease"], now)).fetchone()
        if not current:
            conn.rollback()
            return False
        if payload is None or payload["sequence"] <= current["last_sequence"]:
            failures = min(current["failures"] + 1, 10)
            conn.execute("UPDATE federation_peers SET failures=?, last_error=?, next_attempt=?, lease='', lease_until=0 WHERE wiki_id=?",
                         (failures, "Synchronization failed; check pairing, clock, network and source status.",
                          now + min(3600, 30 * 2**failures), peer["wiki_id"]))
            conn.commit()
            return False
        conn.execute("DELETE FROM federation_copies WHERE peer_id=?", (peer["wiki_id"],))
        conn.executemany("INSERT INTO federation_copies VALUES (?,?,?,?,?,?)",
                         [(peer["wiki_id"], page["id"], page["title"], page["content"], page["source_path"], page["revision"])
                          for page in payload["pages"]])
        conn.execute("UPDATE federation_peers SET last_sequence=?, last_success=?, last_error='', failures=0, "
                     "next_attempt=?, lease='', lease_until=0 WHERE wiki_id=?",
                     (payload["sequence"], now, now + protocol.POLL_SECONDS, peer["wiki_id"]))
        conn.commit()
        return True


def copies(remote_id=None, page_id=None):
    with db.get_db_context() as conn:
        if remote_id is not None:
            return conn.execute("SELECT c.*, p.name, p.base_url, p.audience_category, p.last_success, p.last_error "
                                "FROM federation_copies c JOIN federation_peers p ON p.wiki_id=c.peer_id "
                                "WHERE c.peer_id=? AND c.page_id=?", (remote_id, page_id)).fetchone()
        return conn.execute("SELECT c.peer_id,c.page_id,c.title,p.name,p.audience_category,p.last_success,p.last_error "
                            "FROM federation_copies c JOIN federation_peers p ON p.wiki_id=c.peer_id ORDER BY p.name,c.title").fetchall()
