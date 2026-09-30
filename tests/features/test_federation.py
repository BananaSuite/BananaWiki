"""Federation: pairing, signed snapshots, replay protection, sharing rules, copies, forks and SSRF refusal."""

from __future__ import annotations

import time

import pytest

from bananawiki.core import crypto, http
from bananawiki.core.sqlite import Session
from bananawiki.wiki import accounts, registry
from bananawiki.wiki import db as db_module
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.federation import protocol, store, sync

from .pages_support import make_category, make_page

PASSWORD = "correct horse battery"


class Wiki:
    """One test wiki with its own instance directory."""

    def __init__(self, app_factory, tmp_path, name: str, *, enabled: bool = True):
        self.app = app_factory(environ={"BW_INSTANCE_DIR": str(tmp_path / name),
                                        "BW_FEDERATION_ENABLED": "1" if enabled else "0"})
        self.client = self.app.test_client()
        self.db = Session(self.app.extensions["bananawiki.database"].connect())

    def run(self, fn):
        with self.app.test_request_context(), connection_scope():
            return fn()

    def user(self, username: str, role: str):
        return self.run(lambda: accounts.create(username, PASSWORD, role=role, emit_event=False))

    def login(self, user):
        self.client.post("/login", data={"username": user["username"], "password": PASSWORD})

    @property
    def wiki_id(self) -> str:
        return self.run(store.identity)


@pytest.fixture
def pair(app_factory, tmp_path):
    """Wikis A and B paired both ways; A shows B's pages to readers of its 'Inbox' category."""
    a, b = Wiki(app_factory, tmp_path, "a"), Wiki(app_factory, tmp_path, "b")
    secret = protocol.new_secret()
    inbox = make_category(a.app, "Inbox")
    a.run(lambda: store.pair(b.wiki_id, "Wiki B", "https://b.example", secret, category=inbox["id"]))
    b.run(lambda: store.pair(a.wiki_id, "Wiki A", "https://a.example", secret))
    yield a, b, inbox
    a.db.conn.close()
    b.db.conn.close()


def transport_to(wiki: Wiki, *, tamper: bool = False):
    """A fake HTTP transport that delivers requests to *wiki*'s test client."""
    calls = []

    def send(method, url, *, headers, **options):
        calls.append((method, url, options))
        # The caller holds its own wiki's thread-bound connection; the peer must use its own database.
        caller_session, db_module._thread.session = db_module._thread.session, None
        try:
            response = wiki.client.open(protocol.PATH, method=method, headers=headers)
        finally:
            db_module._thread.session = caller_session
        body = response.data + (b" " if tamper else b"")
        return http.HttpResponse(response.status_code, {k.lower(): v for k, v in response.headers.items()}, body, url)

    send.calls = calls
    return send


def share_page(b: Wiki, title: str = "Shared doc", content: str = "# Hello\nfrom B"):
    editor = b.user("b_editor", "editor")
    page = make_page(b.app, title, content)
    peer_id = b.db.scalar("SELECT wiki_id FROM federation_peers")
    b.run(lambda: store.share(peer_id, page, editor))
    return page, editor


def test_everything_is_404_while_disabled(app_factory, tmp_path):
    wiki = Wiki(app_factory, tmp_path, "off", enabled=False)
    admin = wiki.user("fed_admin", "admin")
    wiki.login(admin)
    for path in ("/federation", "/admin/federation", protocol.PATH):
        assert wiki.client.get(path).status_code == 404
    assert wiki.run(lambda: sync.sync_peer("x", force=True)) is False


def test_pairing_form_validates_and_encrypts_the_key(app_factory, tmp_path):
    wiki = Wiki(app_factory, tmp_path, "solo")
    admin = wiki.user("fed_admin", "admin")
    wiki.login(admin)
    page = wiki.client.get("/admin/federation")
    assert page.status_code == 200 and wiki.wiki_id in page.get_data(as_text=True)
    secret = protocol.new_secret()
    remote = "3f1e2d4c-5b6a-4798-8a9b-0c1d2e3f4a5b"
    for bad in ({"base_url": "http://plain.example"}, {"base_url": "https://user:pw@x.example"},
                {"wiki_id": "not-a-uuid"}, {"secret": "short"}, {"wiki_id": wiki.wiki_id}):
        form = {"name": "Peer", "wiki_id": remote, "base_url": "https://peer.example", "secret": secret, **bad}
        wiki.client.post("/admin/federation", data=form)
        assert wiki.db.scalar("SELECT COUNT(*) FROM federation_peers") == 0, bad
    wiki.client.post("/admin/federation", data={"name": "Peer", "wiki_id": remote, "base_url": "https://peer.example",
                                                "secret": secret})
    stored = wiki.db.scalar("SELECT secret FROM federation_peers")
    assert crypto.is_encrypted(stored) and secret not in stored
    wiki.client.post("/admin/federation", data={"name": "Again", "wiki_id": remote, "base_url": "https://o.example",
                                                "secret": secret})
    assert wiki.db.scalar("SELECT COUNT(*) FROM federation_peers") == 1


def test_non_admins_cannot_manage_pairings(app_factory, tmp_path):
    wiki = Wiki(app_factory, tmp_path, "solo")
    wiki.login(wiki.user("fed_editor", "editor"))
    assert wiki.client.get("/admin/federation").status_code in (302, 403)
    assert wiki.client.get("/federation/sharing").status_code == 200


def test_sync_stores_copies_and_readers_see_them(pair):
    a, b, inbox = pair
    share_page(b)
    fake = transport_to(b)
    assert a.run(lambda: sync.sync_peer(b.wiki_id, force=True, transport=fake)) is True
    assert fake.calls[0][1] == "https://b.example" + protocol.PATH and fake.calls[0][2]["allow_private"] is False
    copy = a.db.one("SELECT * FROM federation_copies")
    assert copy["title"] == "Shared doc" and copy["source_path"] == "/page/shared-doc"

    outsider = a.user("outsider", "user")
    a.db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 1)",
                 (outsider["id"],))
    a.login(outsider)
    assert "Shared doc" not in a.client.get("/federation").get_data(as_text=True)
    assert a.client.get(f"/federation/copies/{b.wiki_id}/{copy['page_id']}").status_code == 404
    a.client.post("/logout")
    reader = a.user("inbox_reader", "user")
    a.login(reader)
    assert "Shared doc" in a.client.get("/federation").get_data(as_text=True)
    view = a.client.get(f"/federation/copies/{b.wiki_id}/{copy['page_id']}")
    assert view.status_code == 200 and "# Hello" in view.get_data(as_text=True)
    assert view.headers["Cache-Control"] == "no-store" and view.headers["Referrer-Policy"] == "no-referrer"


def test_stale_copies_are_hidden(pair):
    a, b, _inbox = pair
    share_page(b)
    a.run(lambda: sync.sync_peer(b.wiki_id, force=True, transport=transport_to(b)))
    a.db.execute("UPDATE federation_peers SET last_success = ?", (time.time() - store.STALE_AFTER - 10,))
    admin = a.user("a_admin", "admin")
    a.login(admin)
    assert "Shared doc" not in a.client.get("/federation").get_data(as_text=True)


def test_fork_creates_a_real_page_with_events(pair):
    a, b, inbox = pair
    share_page(b)
    a.run(lambda: sync.sync_peer(b.wiki_id, force=True, transport=transport_to(b)))
    seen = []
    a.app.extensions["bananawiki.registry"].add(registry.Feature(
        id="probe", name="p", toggle="always", events={"page.created": [lambda page, author_id: seen.append(page)]}))
    admin = a.user("a_admin", "admin")
    a.login(admin)
    page_id = a.db.scalar("SELECT page_id FROM federation_copies")
    response = a.client.post(f"/federation/copies/{b.wiki_id}/{page_id}/fork")
    assert response.status_code == 302
    forked = a.db.one("SELECT * FROM pages WHERE slug LIKE 'federated-%'")
    assert forked["category_id"] == inbox["id"] and "Source wiki: " + b.wiki_id in forked["content"]
    assert a.db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (forked["id"],)) == 1
    assert [page["id"] for page in seen] == [forked["id"]]


def test_replayed_or_forged_requests_are_refused(pair):
    a, b, _inbox = pair
    secret = a.run(lambda: store.secret_of(store.peer(b.wiki_id)))
    headers = protocol.request_headers(a.wiki_id, b.wiki_id, secret)
    assert b.client.get(protocol.PATH, headers=headers).status_code == 200
    assert b.client.get(protocol.PATH, headers=headers).status_code == 401  # same nonce
    b.db.execute("UPDATE federation_peers SET last_served = 0")
    forged = {**protocol.request_headers(a.wiki_id, b.wiki_id, protocol.new_secret())}
    assert b.client.get(protocol.PATH, headers=forged).status_code == 401
    old = protocol.request_headers(a.wiki_id, b.wiki_id, secret, now=time.time() - 3600)
    assert b.client.get(protocol.PATH, headers=old).status_code == 401
    fresh = protocol.request_headers(a.wiki_id, b.wiki_id, secret)
    assert b.client.get(protocol.PATH, headers=fresh).status_code == 200
    busy = protocol.request_headers(a.wiki_id, b.wiki_id, secret)
    assert b.client.get(protocol.PATH, headers=busy).status_code == 429
    assert b.client.get(protocol.PATH + "?x=1", headers=busy).status_code == 400


def test_tampered_responses_are_rejected(pair):
    a, b, _inbox = pair
    share_page(b)
    assert a.run(lambda: sync.sync_peer(b.wiki_id, force=True, transport=transport_to(b, tamper=True))) is False
    peer = a.db.one("SELECT * FROM federation_peers")
    assert peer["last_error"] == "federation.error.response_signature" and peer["failures"] == 1
    assert peer["next_attempt"] > time.time() and a.db.scalar("SELECT COUNT(*) FROM federation_copies") == 0


def test_private_addresses_are_refused_unless_allowed(app_factory, tmp_path):
    wiki = Wiki(app_factory, tmp_path, "solo")
    remote = "3f1e2d4c-5b6a-4798-8a9b-0c1d2e3f4a5b"
    wiki.run(lambda: store.pair(remote, "Local", "https://127.0.0.1:9", protocol.new_secret()))
    assert wiki.run(lambda: sync.sync_peer(remote, force=True)) is False
    assert wiki.db.scalar("SELECT last_error FROM federation_peers") == "federation.error.blocked_address"
    wiki.db.execute("UPDATE federation_peers SET allow_private_network = 1, lease_until = 0")
    wiki.run(lambda: sync.sync_peer(remote, force=True))
    assert wiki.db.scalar("SELECT last_error FROM federation_peers") == "federation.error.network"


def test_sharing_rules(pair):
    a, b, _inbox = pair
    peer_id = b.db.scalar("SELECT wiki_id FROM federation_peers")
    reader = b.user("b_reader", "user")
    page = make_page(b.app, "Plain", "text")
    with pytest.raises(protocol.ProtocolError):
        b.run(lambda: store.share(peer_id, page, reader))
    editor = b.user("b_editor", "editor")
    big = make_page(b.app, "Big", "x" * (protocol.MAX_CONTENT + 1))
    with pytest.raises(protocol.ProtocolError):
        b.run(lambda: store.share(peer_id, big, editor))
    b.login(editor)
    b.client.post("/federation/sharing", data={"peer_id": peer_id, "slug": "plain"})
    assert b.db.scalar("SELECT COUNT(*) FROM federation_shares") == 1
    other = b.user("b_other", "editor")
    assert b.run(lambda: store.unshare(peer_id, page["id"], other)) is False
    b.db.execute("UPDATE pages SET is_deindexed = 1 WHERE id = ?", (page["id"],))
    b.run(lambda: store.snapshot(protocol.request_headers(
        a.wiki_id, b.wiki_id, b.run(lambda: store.secret_of(store.peer(peer_id))))))
    assert b.db.scalar("SELECT COUNT(*) FROM federation_shares") == 0  # withdrawn once no longer shareable


def test_disconnect_removes_copies_and_legacy_keys_are_encrypted(pair):
    a, b, _inbox = pair
    share_page(b)
    a.run(lambda: sync.sync_peer(b.wiki_id, force=True, transport=transport_to(b)))
    plain = protocol.new_secret()
    a.db.execute("UPDATE federation_peers SET secret = ?", (plain,))
    a.run(store.encrypt_legacy_secrets)
    assert a.run(lambda: store.secret_of(store.peer(b.wiki_id))) == plain
    admin = a.user("a_admin", "admin")
    a.login(admin)
    a.client.post(f"/admin/federation/{b.wiki_id}/delete")
    assert a.db.scalar("SELECT COUNT(*) FROM federation_copies") == 0


def test_protocol_validation():
    source = "3f1e2d4c-5b6a-4798-8a9b-0c1d2e3f4a5b"
    page = {"id": "0c1d2e3f-4a5b-4798-8a9b-3f1e2d4c5b6a", "title": "T", "content": "c", "source_path": "/page/t"}
    page["revision"] = protocol.revision("T", "c", "/page/t")
    assert protocol.validate_snapshot({"version": 1, "wiki_id": source, "sequence": 1, "pages": [page]}, source)
    for broken in ({**page, "revision": "0" * 64}, {**page, "source_path": "https://evil/page/t"}):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_snapshot({"version": 1, "wiki_id": source, "sequence": 1, "pages": [broken]}, source)
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_snapshot({"version": 1, "wiki_id": source, "sequence": 0, "pages": []}, source)
