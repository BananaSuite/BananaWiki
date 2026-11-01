# ruff: noqa: F811 - the hosting fixtures are imported by name
"""Regression tests for the n5 findings of the full re-audit pass 8 (hosting portal: wiki lifecycle, fallback proxy)."""

from __future__ import annotations

from bananawiki.core.timeutil import now_sql
from bananawiki.hosting.db import connection_scope
from tests.hosting.conftest import login, make_account, portal, query, runtime, web  # noqa: F401

# NW4-1: wikis an owner terminated hold their place while their data is kept, and creations per day are bounded

USE = "A wiki for our class notes and homework planning."
MIB = 1024 * 1024


def _create(client, slug: str, addr: str):
    return client.post("/instances/create", data={"subdomain": slug, "declared_use_case": USE,
                                                  "compliance_declared": "1"}, environ_base={"REMOTE_ADDR": addr})


def _wiki(query, slug: str):
    return query("SELECT * FROM instances WHERE subdomain = ? OR subdomain LIKE ?", (slug, f"{slug}--%"), one=True)


def _newcomer(make_account):
    """A self-service account still on probation: it may hold one wiki."""
    return make_account(terms_accepted_at=now_sql(), approved_by=None, created_at=now_sql())


def test_nw4_1_a_terminated_wiki_holds_its_place_until_its_owner_deletes_its_data(portal, runtime, make_account,
                                                                                  login, query):
    owner = _newcomer(make_account)
    client = portal.test_client()
    login(client, owner)
    assert _create(client, "first", "198.51.100.1").status_code == 200
    first = _wiki(query, "first")
    assert client.post(f"/instances/{first['id']}/terminate").status_code == 302
    kept = _wiki(query, "first")
    assert kept["status"] == "terminated" and kept["data_retained_until"]
    assert len(runtime.tenants) == 1
    refused = _create(client, "second", "198.51.100.2")
    assert refused.status_code == 400
    assert "count until their data is deleted" in refused.get_data(as_text=True)
    assert _wiki(query, "second") is None
    # The owner sees the terminated wiki (downloads are off) and deletes its data to free the place.
    listing = client.get("/dashboard")
    assert listing.status_code == 200 and f"/instances/{first['id']}" in listing.get_data(as_text=True)
    page = client.get(f"/instances/{first['id']}")
    assert page.status_code == 200 and "Delete data now" in page.get_data(as_text=True)
    assert client.post(f"/instances/{first['id']}/delete-data").status_code == 302
    gone = _wiki(query, "first")
    assert gone["status"] == "terminated" and gone["data_retained_until"] is None
    assert runtime.tenants == {}
    assert _create(client, "second", "198.51.100.3").status_code == 200
    assert _wiki(query, "second")["status"] == "running"


def test_nw4_1_a_paused_countdown_keeps_the_data_from_its_owner(portal, runtime, make_account, login,
                                                                query):
    owner = make_account()
    client = portal.test_client()
    login(client, owner)
    assert _create(client, "held", "198.51.100.1").status_code == 200
    held = _wiki(query, "held")
    assert client.post(f"/instances/{held['id']}/terminate").status_code == 302
    query("UPDATE instances SET grace_period_suspended = 1 WHERE id = ?", (held["id"],))
    assert client.post(f"/instances/{held['id']}/delete-data").status_code == 404
    assert _wiki(query, "held")["data_retained_until"] and len(runtime.tenants) == 1
    # Someone else's wiki is not theirs to delete either.
    other = make_account()
    elsewhere = portal.test_client()
    login(elsewhere, other)
    query("UPDATE instances SET grace_period_suspended = 0 WHERE id = ?", (held["id"],))
    assert elsewhere.post(f"/instances/{held['id']}/delete-data").status_code == 404
    assert len(runtime.tenants) == 1


def test_nw4_1_creating_and_deleting_wikis_in_a_loop_stops_at_the_daily_budget(portal, runtime, make_account,
                                                                               login, query, monkeypatch):
    from bananawiki.hosting import certwarm

    warmed = []
    monkeypatch.setattr(certwarm, "schedule", lambda host: warmed.append(host))
    owner = _newcomer(make_account)  # one place: two creations a day
    client = portal.test_client()
    login(client, owner)
    for n in range(2):
        assert _create(client, f"loop{n}", f"198.51.100.{n + 1}").status_code == 200
        wiki = _wiki(query, f"loop{n}")
        assert client.post(f"/instances/{wiki['id']}/terminate").status_code == 302
        assert client.post(f"/instances/{wiki['id']}/delete-data").status_code == 302
    refused = _create(client, "loop2", "198.51.100.9")
    assert refused.status_code == 400
    assert "at most 2 wikis in 24 hours" in refused.get_data(as_text=True)
    assert _wiki(query, "loop2") is None
    assert len(warmed) == 2
    # The budget is per 24 hours.
    query("UPDATE instances SET created_at = datetime(created_at, '-25 hours') WHERE account_id = ?", (owner["id"],))
    query("UPDATE hosting_events SET created_at = datetime(created_at, '-25 hours') WHERE actor_id = ?",
          (owner["id"],))
    assert _create(client, "loop2", "198.51.100.10").status_code == 200


def test_nw4_1_wikis_given_away_still_count_towards_their_creators_day(portal, runtime, make_account, login, query,
                                                                       monkeypatch):
    from bananawiki.hosting import certwarm

    warmed = []
    monkeypatch.setattr(certwarm, "schedule", lambda host: warmed.append(host))
    creator = _newcomer(make_account)  # two creations a day
    helper = make_account()
    ours, theirs = portal.test_client(), portal.test_client()
    login(ours, creator)
    login(theirs, helper)
    for n in range(2):
        assert _create(ours, f"gift{n}", f"198.51.100.{n + 1}").status_code == 200
        wiki = _wiki(query, f"gift{n}")
        ours.post(f"/instances/{wiki['id']}/transfer", data={"username": helper["username"]},
                  environ_base={"REMOTE_ADDR": f"198.51.101.{n + 1}"})
        offer = query("SELECT id FROM instance_ownership_transfers WHERE instance_id = ? AND status = 'pending'",
                      (wiki["id"],), one=True)
        theirs.post(f"/transfers/{offer['id']}/accept", environ_base={"REMOTE_ADDR": f"198.51.102.{n + 1}"})
        assert _wiki(query, f"gift{n}")["account_id"] == helper["id"]
        assert theirs.post(f"/instances/{wiki['id']}/terminate").status_code == 302
        assert theirs.post(f"/instances/{wiki['id']}/delete-data").status_code == 302
    refused = _create(ours, "gift2", "198.51.100.9")
    assert refused.status_code == 400
    assert "at most 2 wikis in 24 hours" in refused.get_data(as_text=True)
    assert _wiki(query, "gift2") is None and len(warmed) == 2


def test_nw4_1_wikis_kept_after_their_account_was_deleted_fill_the_newcomers_share(portal, runtime, make_account,
                                                                                    login, query):
    from bananawiki.hosting import settings
    from tests.hosting.conftest import PASSWORD

    database = portal.extensions["bananawiki.hosting.database"]
    with portal.app_context(), connection_scope(database):
        settings.update(global_limit_enabled=1, global_limit_max_instances=4)  # newcomers share 2 of them
    for n in range(2):  # sign up, create a wiki, delete the account
        client = portal.test_client()
        login(client, _newcomer(make_account))
        assert _create(client, f"gone{n}", f"198.51.100.{n + 1}").status_code == 200
        client.post("/account/delete", data={"current_password": PASSWORD},
                    environ_base={"REMOTE_ADDR": f"198.51.101.{n + 1}"})
        kept = _wiki(query, f"gone{n}")
        assert kept["status"] == "terminated" and kept["data_retained_until"]
    assert len(runtime.tenants) == 2
    client = portal.test_client()
    login(client, _newcomer(make_account))
    refused = _create(client, "third", "198.51.100.9")
    assert refused.status_code == 400 and _wiki(query, "third") is None
    # The platform's own count sees them as well; an established account takes the last places.
    established = portal.test_client()
    login(established, make_account())
    assert _create(established, "fourth", "192.0.2.1").status_code == 200
    assert _create(established, "fifth", "192.0.2.2").status_code == 200
    assert _create(established, "sixth", "192.0.2.3").status_code == 400
    # Once an administrator pauses a countdown, that data is the administrator's to keep: it holds no place.
    query("UPDATE instances SET grace_period_suspended = 1 WHERE subdomain LIKE 'gone0--%'")
    assert _create(established, "sixth", "192.0.2.4").status_code == 200


def test_nw4_1_a_wiki_terminated_on_the_command_line_frees_its_owners_place(portal, runtime, make_account, login,
                                                                             query, capsys):
    from bananawiki.hosting import admin

    owner = _newcomer(make_account)
    client = portal.test_client()
    login(client, owner)
    assert _create(client, "cli", "198.51.100.1").status_code == 200
    wiki = _wiki(query, "cli")
    assert admin.main(["instance", "terminate", wiki["id"], "--yes"], app=portal) == 0
    capsys.readouterr()
    assert _wiki(query, "cli")["terminated_reason"] == "admin"  # an administrator's decision, as on the console
    assert _create(client, "again", "198.51.100.2").status_code == 200


def test_nw4_1_platform_storage_counts_the_data_of_terminated_wikis(portal, runtime, make_account, login,
                                                                    query, monkeypatch):
    from bananawiki.hosting import instances, settings

    monkeypatch.setattr(type(runtime), "usage", lambda self, spec, max_age=None: 400 * MIB
                        if spec.data_dir_name in self.tenants else 0)
    owner = make_account()
    client = portal.test_client()
    login(client, owner)
    for n in range(2):
        assert _create(client, f"big{n}", "198.51.100.1").status_code == 200
        wiki = _wiki(query, f"big{n}")
        assert client.post(f"/instances/{wiki['id']}/terminate").status_code == 302
    database = portal.extensions["bananawiki.hosting.database"]
    with portal.app_context(), connection_scope(database):
        assert instances.platform_usage_mb() == 800
        settings.update(global_limit_enabled=1, global_limit_max_storage_mb=700)
    other = make_account()
    elsewhere = portal.test_client()
    login(elsewhere, other)
    refused = _create(elsewhere, "third", "192.0.2.9")
    assert refused.status_code == 400 and _wiki(query, "third") is None


# NW4-2: the wiki-host fallback proxy never parks more threads than its waiting places, and is rate-limited

def _held_upstream():
    """An upstream wiki that holds every request until *release* is set."""
    import http.server
    import threading

    arrived, release = [], threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            arrived.append(self.path)
            release.wait(10)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, arrived, release


def test_nw4_2_a_request_for_a_slow_wiki_with_no_waiting_place_is_turned_away_at_once(portal, runtime, make_account,
                                                                                     monkeypatch):
    import threading
    import time

    from bananawiki.hosting import instances, wikihosts

    monkeypatch.setattr(wikihosts, "QUEUE_SECONDS", 1.0)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.create(make_account(), "slow")
    server, arrived, release = _held_upstream()
    runtime.upstreams["slow"] = server.server_address
    host = {"Host": "slow-hosting.wiki.test"}
    answers: list[int] = []

    def visit(n: int) -> None:
        response = portal.test_client().get(f"/r{n}", headers=host, environ_base={"REMOTE_ADDR": f"203.0.113.{n}"})
        answers.append(response.status_code)
        response.close()  # frees its slot for the next one

    try:
        # Two requests proxied (the wiki's slots), then three waiting (its waiting places).
        threads = [threading.Thread(target=visit, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 3
        while len(arrived) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(arrived) == 2
        waiting = [threading.Thread(target=visit, args=(n,)) for n in range(2, 5)]
        for thread in waiting:
            thread.start()
        time.sleep(0.2)
        started = time.monotonic()
        late = portal.test_client().get("/late", headers=host, environ_base={"REMOTE_ADDR": "203.0.113.99"})
        elapsed = time.monotonic() - started
        assert late.status_code == 503 and "busy" in late.get_data(as_text=True).lower()
        assert elapsed < 0.5, f"held a worker thread for {elapsed:.2f} s"
    finally:
        release.set()
        for thread in threads + waiting:
            thread.join(5)
        server.shutdown()
        server.server_close()
    assert answers.count(200) == 5


def test_nw4_2_waiting_places_are_bounded_per_wiki_and_leave_the_others_a_slot():
    import threading
    import time

    from bananawiki.hosting import wikihosts

    slots = wikihosts._Slots(total=3, waiting=4)
    active = [slots.get("slow") for _ in range(2)]
    assert all(slot.acquire(0.01) for slot in active)
    results: list[bool] = []

    def wait_for_slot() -> None:
        slot = slots.get("slow")
        results.append(slot.acquire(2))
        if results[-1]:
            slot.release()

    waiters = [threading.Thread(target=wait_for_slot) for _ in range(3)]
    for thread in waiters:
        thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    assert not slots.get("slow").acquire(3)  # no waiting place left for this wiki: refused at once
    assert time.monotonic() - started < 0.5
    other = slots.get("other")
    assert other.acquire(0.01)  # the process keeps a slot for every other wiki
    other.release()
    for slot in active:
        slot.release()
    for thread in waiters:
        thread.join(5)
    assert results == [True, True, True]


def test_nw4_2_the_portal_runs_a_thread_for_each_waiting_place(monkeypatch):
    import runpy
    from pathlib import Path

    from bananawiki.core import downloads
    from bananawiki.hosting import wikihosts

    conf = Path(__file__).resolve().parents[2] / "hosting" / "gunicorn.conf.py"
    monkeypatch.delenv("HOSTING_THREADS", raising=False)
    assert runpy.run_path(str(conf))["threads"] == 4 + downloads.PORTAL_PLACES + wikihosts.WAITING_PLACES
    # The proxy's own count of threads is capped like Gunicorn's, so it always leaves one to the portal.
    monkeypatch.setenv("HOSTING_THREADS", "50")
    assert runpy.run_path(str(conf))["threads"] == 32 + downloads.PORTAL_PLACES + wikihosts.WAITING_PLACES
    assert wikihosts._Slots()._pool._value == 31


def test_nw4_2_wiki_host_requests_are_limited_per_network(web, monkeypatch):
    from bananawiki.hosting import wikihosts

    monkeypatch.setattr(wikihosts, "RATE_LIMIT", 3)
    host = {"Host": "nowiki-hosting.wiki.test"}
    statuses = [web.get("/", headers=host, environ_base={"REMOTE_ADDR": "198.51.100.7"}).status_code
                for _ in range(4)]
    assert statuses == [404, 404, 404, 429]
    limited = web.get("/", headers=host, environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    assert "Too many requests" in limited.get_data(as_text=True)
    assert web.get("/", headers=host, environ_base={"REMOTE_ADDR": "198.51.100.8"}).status_code == 404
    # The portal's own pages keep their own budget.
    assert web.get("/login", environ_base={"REMOTE_ADDR": "198.51.100.7"}).status_code == 200


def _proxy_of(portal):
    from bananawiki.hosting import wikihosts

    layer = portal.wsgi_app
    while not isinstance(layer, wikihosts.WikiHosts):
        layer = getattr(layer, "wsgi_app", None) or layer.app
    return layer


def test_nw4_2_a_retiring_worker_cuts_off_proxied_and_waiting_requests_at_once(portal, runtime, make_account,
                                                                               monkeypatch):
    import threading
    import time

    from bananawiki.hosting import instances, wikihosts

    monkeypatch.setattr(wikihosts, "QUEUE_SECONDS", 8.0)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.create(make_account(), "slow")
    server, arrived, release = _held_upstream()
    runtime.upstreams["slow"] = server.server_address
    host = {"Host": "slow-hosting.wiki.test"}
    answers: list[tuple[int, float]] = []

    def visit(n: int) -> None:
        started = time.monotonic()
        response = portal.test_client().get(f"/r{n}", headers=host, environ_base={"REMOTE_ADDR": f"203.0.113.{n}"})
        answers.append((response.status_code, time.monotonic() - started))
        response.close()

    threads = [threading.Thread(target=visit, args=(n,)) for n in range(3)]  # two proxied, one waiting
    try:
        for thread in threads[:2]:
            thread.start()
        deadline = time.monotonic() + 3
        while len(arrived) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        threads[2].start()
        time.sleep(0.3)
        assert answers == []
        started = time.monotonic()
        _proxy_of(portal).slots.drain()  # what Gunicorn's retiring of the worker triggers
        for thread in threads:
            thread.join(3)
        assert time.monotonic() - started < 1.5, "the drain waited for the slow wiki"
        assert sorted(code for code, _ in answers) == [503, 503, 503]
        late = portal.test_client().get("/late", headers=host, environ_base={"REMOTE_ADDR": "203.0.113.99"})
        assert late.status_code == 503 and len(arrived) == 2
    finally:
        release.set()
        for thread in threads:
            if thread.is_alive():
                thread.join(5)
        server.shutdown()
        server.server_close()


def test_nw4_2_the_portal_drains_the_proxy_when_gunicorn_retires_a_worker(monkeypatch):
    import runpy
    import time
    from pathlib import Path

    from bananawiki.hosting import wikihosts

    drained = []
    monkeypatch.setattr(wikihosts, "drain", lambda: drained.append(True))
    monkeypatch.setattr(wikihosts, "DRAIN_CHECK_SECONDS", 0.01)
    conf = runpy.run_path(str(Path(__file__).resolve().parents[2] / "hosting" / "gunicorn.conf.py"))

    class Worker:
        alive = True
        wsgi = staticmethod(lambda environ, start_response: [])

    worker = Worker()
    conf["post_worker_init"](worker)
    time.sleep(0.1)
    assert drained == []
    worker.alive = False  # max_requests reached, or SIGTERM
    deadline = time.monotonic() + 2
    while not drained and time.monotonic() < deadline:
        time.sleep(0.01)
    assert drained == [True]


def test_nw4_2_a_retiring_worker_cuts_off_a_trickling_wiki_response(portal, runtime, make_account):
    import http.server
    import threading
    import time

    from bananawiki.hosting import instances

    release = threading.Event()

    class Trickle(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            self.wfile.flush()
            release.wait(10)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        instances.create(make_account(), "drip")
    runtime.upstreams["drip"] = server.server_address
    try:
        response = portal.test_client().get("/", headers={"Host": "drip-hosting.wiki.test"}, buffered=False)
        assert response.status_code == 200
        threading.Timer(0.3, _proxy_of(portal).slots.drain).start()
        started = time.monotonic()
        body = b"".join(response.response)
        assert time.monotonic() - started < 1.5 and body == b"x" * 10
        response.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
