"""Regression tests for the hosting findings of the security re-audit (AUDIT.md, R3-*)."""

from __future__ import annotations

from bananawiki.hosting import email
from bananawiki.hosting import instances as instances_mod


def test_r3_1_a_reset_link_dies_when_the_password_changes(web, portal, make_account, login):
    user = make_account(email="victim@example.org", email_verified_at="2026-01-01 00:00:00")
    attacker = portal.test_client()
    with email.capture_outbox() as outbox:
        attacker.post("/forgot-password", data={"username": user["username"], "email": "victim@example.org"})
    token = outbox[0].action_url.split("token=", 1)[1]
    login(web, user)
    assert web.post("/account/change-password", data={
        "current_password": "correct horse 42", "new_password": "victim new 99",
        "confirm_new_password": "victim new 99"}).status_code == 302

    attacker.post("/reset-password", data={"token": token, "new_password": "attacker 1234",
                                           "confirm_new_password": "attacker 1234"})

    other = portal.test_client()
    assert other.post("/login", data={"username": user["username"], "password": "attacker 1234"}).status_code != 302


def test_r3_2_a_suspension_during_acceptance_keeps_the_wiki(portal, make_account, make_wiki, login, query,
                                                            monkeypatch):
    abuser, sock = make_account(), make_account()
    wiki = make_wiki(abuser)
    owner = portal.test_client()
    login(owner, abuser)
    assert owner.post(f"/instances/{wiki['id']}/transfer", data={"username": sock["username"]}).status_code == 302
    transfer_id = query("SELECT id FROM instance_ownership_transfers WHERE status='pending'", one=True)["id"]
    real, fired = instances_mod.require_ready, []

    def administrator_acts_meanwhile(inst):
        if not fired:  # another worker suspends the owner between the checks and the move
            fired.append(1)
            from bananawiki.hosting import accounts
            accounts.suspend(abuser["id"], until=None, reason="spam", reason_visible=False,
                             time_visible=False, actor_id=None, duration_label="permanent")
        return real(inst)

    monkeypatch.setattr(instances_mod, "require_ready", administrator_acts_meanwhile)
    recipient = portal.test_client()
    login(recipient, sock)
    recipient.post(f"/transfers/{transfer_id}/accept")

    assert fired
    assert query("SELECT account_id FROM instances WHERE id = ?", (wiki["id"],), one=True)["account_id"] == abuser["id"]
    assert query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (transfer_id,),
                 one=True)["status"] == "cancelled"


def test_s1_2_a_decline_during_acceptance_cannot_leave_the_wiki_half_moved(portal, runtime, ctx, make_account,
                                                                           make_wiki, query):
    from bananawiki.hosting import collaborators, instances

    admin, bob = make_account("root", admin=True), make_account("bob")
    wiki = make_wiki(admin, "landing", domain_mode="apex")
    collaborators.start_transfer(wiki, admin, "bob")
    transfer = query("SELECT id FROM instance_ownership_transfers WHERE status = 'pending'", one=True)["id"]
    original, refused = runtime.relocate, []

    def relocate_while_declining(*args, **kwargs):
        original(*args, **kwargs)
        try:  # bob's other request, while the accept is moving the wiki
            collaborators.resolve_transfer(transfer, bob["id"], as_sender=False)
        except Exception as error:  # noqa: BLE001 - recorded below
            refused.append(error)

    runtime.relocate = relocate_while_declining
    collaborators.accept_transfer(transfer, bob)

    after = instances.get(wiki["id"])
    assert after["account_id"] == bob["id"] and after["status"] == "running"
    assert query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (transfer,), one=True)["status"] \
        == "accepted"


def test_t1_3_an_invalid_upstream_header_does_not_leak_proxy_slots(portal):
    import socket
    import threading

    from gunicorn.http.message import TOKEN_RE
    from gunicorn.http.wsgi import HEADER_VALUE_RE

    from bananawiki.hosting.wikihosts import WikiHosts

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def serve():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            connection.recv(65536)
            connection.sendall(b"HTTP/1.1 200 OK\r\nX-Plugin: a\r\n b\r\nX-Bad\x01: c\r\nContent-Length: 2\r\n\r\nok")
            connection.close()

    threading.Thread(target=serve, daemon=True).start()

    def gunicorn_start_response(status, headers, exc_info=None):  # gunicorn 26 refuses these
        for name, value in headers:
            assert TOKEN_RE.fullmatch(name) and HEADER_VALUE_RE.fullmatch(value), (name, value)

    hosts = WikiHosts(portal, portal.wsgi_app)
    free = hosts.slots._pool._value
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "RAW_URI": "/", "HTTP_HOST": "evil.example.org",
               "wsgi.input": None, "REMOTE_ADDR": "203.0.113.9", "wsgi.url_scheme": "https"}
    for _ in range(free + 1):
        body = hosts._proxy(environ, gunicorn_start_response, "evil.example.org", "evil-a", "127.0.0.1",
                            server.getsockname()[1])
        b"".join(body)
        getattr(body, "close", lambda: None)()
    server.close()
    assert hosts.slots._pool._value == free


def test_t1_2_a_suspension_during_a_restart_keeps_the_wiki_down(portal, runtime, make_account, make_wiki, login,
                                                               query):
    from bananawiki.hosting import instances, urls
    from bananawiki.hosting.db import connection_scope

    owner, admin = make_account(), make_account("root", admin=True)
    wiki = make_wiki(owner)
    name = urls.data_dir_name(wiki["subdomain"], "hosting")
    real_restart, fired = runtime.restart, []

    def restart_while_an_administrator_suspends(spec, **kwargs):
        # The agent's restart is a stop then a start; the suspension (another worker) lands in between.
        runtime.tenants[spec.data_dir_name].running = False
        if not fired:
            fired.append(1)
            with portal.test_request_context("/"), \
                    connection_scope(portal.extensions["bananawiki.hosting.database"]):
                instances.suspend(instances.get(wiki["id"]), actor_id=admin["id"], reason="phishing")
        return real_restart(spec, **kwargs)

    runtime.restart = restart_while_an_administrator_suspends
    web = portal.test_client()
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/restart")

    assert fired
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "suspended"
    assert runtime.tenants[name].running is False


def test_t1_1_a_wiki_created_during_acceptance_cannot_leave_the_transfer_half_done(portal, runtime, ctx,
                                                                                 make_account, make_wiki, query):
    from bananawiki.hosting import collaborators, instances
    from bananawiki.hosting.errors import ServiceError

    admin, bob = make_account("root", admin=True), make_account("bob")
    wiki = make_wiki(admin, "landing", domain_mode="apex")
    for n in range(instances.account_limit(bob) - 1):
        instances.create(bob, f"bobhas{n}")
    collaborators.start_transfer(wiki, admin, "bob")
    transfer = query("SELECT id FROM instance_ownership_transfers WHERE status = 'pending'", one=True)["id"]
    original, refused = runtime.relocate, []

    def relocate_while_creating(*args, **kwargs):
        original(*args, **kwargs)
        try:  # bob's other request: one more wiki of his own while the accept renames the admin's
            instances.create(bob, "bobextra")
        except ServiceError as error:
            refused.append(error.key)

    runtime.relocate = relocate_while_creating
    collaborators.accept_transfer(transfer, bob)

    assert refused  # the place was held for the wiki on its way
    assert instances.get(wiki["id"])["account_id"] == bob["id"]
    assert query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (transfer,), one=True)["status"] \
        == "accepted"


def test_u1_1_a_finished_transfer_frees_the_recipients_place(portal, runtime, ctx, make_account, make_wiki, query):
    from bananawiki.core.timeutil import sql_in
    from bananawiki.hosting import collaborators, instances

    alice, bob, carol = make_account("alice"), make_account("bob"), make_account("carol")
    wiki = make_wiki(alice, "shared")
    collaborators.start_transfer(wiki, alice, "bob")
    first = query("SELECT id FROM instance_ownership_transfers WHERE status = 'pending'", one=True)["id"]
    collaborators.accept_transfer(first, bob)
    collaborators.start_transfer(instances.get(wiki["id"]), bob, "carol")
    second = query("SELECT id FROM instance_ownership_transfers WHERE status = 'pending'", one=True)["id"]
    collaborators.accept_transfer(second, carol)
    query("UPDATE instance_ownership_transfers SET resolved_at = ? WHERE id = ?",
          (sql_in(minutes=-(instances.ACCEPTING_MINUTES + 1)), first))

    for n in range(instances.account_limit(bob)):
        instances.create(bob, f"bobown{n}")  # bob owns nothing: every place is his


def test_u1_2_a_policy_change_while_a_wiki_is_made_leaves_it_running(portal, runtime, ctx, make_account, query):
    from bananawiki.hosting import instances, settings

    alice = make_account("alice")
    original = runtime.provision

    def provision_while_an_administrator_changes_settings(*args, **kwargs):
        result = original(*args, **kwargs)
        settings.update(global_tour_enabled=0 if settings.flag("global_tour_enabled") else 1)
        return result

    runtime.provision = provision_while_an_administrator_changes_settings
    inst, _username, _password = instances.create(alice, "fresh")

    assert query("SELECT status FROM instances WHERE id = ?", (inst["id"],), one=True)["status"] == "running"
    assert runtime.tenants[instances.spec(instances.get(inst["id"])).data_dir_name].running


def test_u1_4_maintenance_recovery_keeps_a_wiki_suspended_meanwhile_down(portal, runtime, ctx, make_account,
                                                                        make_wiki, query):
    from bananawiki.hosting import instances, maintenance

    wiki = make_wiki(make_account("tenant"), "noisy")
    name = instances.spec(wiki).data_dir_name
    runtime.tenants[name].running = False  # crashed, so the next pass recovers it
    original = runtime.recover

    def recover_while_an_administrator_suspends(specs):
        instances.suspend(instances.get(wiki["id"]), actor_id=None, reason="abuse")
        return original(specs)

    runtime.recover = recover_while_an_administrator_suspends
    maintenance._restart_downed(portal)

    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "suspended"
    assert runtime.tenants[name].running is False


def test_u1_5_the_proxy_sends_its_own_status_phrase():
    from bananawiki.hosting.wikihosts import _status_line

    assert _status_line(200) == "200 OK"
    assert _status_line(299) == "299 Unknown"
