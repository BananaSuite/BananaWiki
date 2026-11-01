"""Regression tests for the HW2 findings of the full re-audit pass 3 (hosting portal, ops, contrib)."""

from __future__ import annotations

import base64
import dataclasses
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.hosting.conftest import login, make_account, make_wiki, portal, query, runtime, web  # noqa: F401
from tests.hosting.test_cross_wiki_sign_in import _signed_in, platform  # noqa: F401
from tests.security.test_fw2 import _platform_sign_in

# HW2-1: the server-to-server OAuth endpoints count failures and each wiki's calls, not the wiki's address


def _bogus_callback(platform, addr: str) -> int:  # noqa: F811
    """An anonymous visitor makes the wiki redeem a code they made up."""
    visitor = platform["wiki"].test_client()
    visitor.environ_base["REMOTE_ADDR"] = addr
    start = visitor.get("/platform-oauth/login", base_url=platform["url"])
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    return visitor.get(f"/platform-oauth/callback?state={state}&code=bogus", base_url=platform["url"]).status_code


def _hits(query, bucket: str) -> int:  # noqa: F811
    return query("SELECT COUNT(*) AS n FROM hosting_rate_limit_hits WHERE bucket = ?", (bucket,), one=True)["n"]


def test_hw2_1_bogus_callbacks_do_not_use_up_the_wikis_sign_ins(platform, make_account, login, query,  # noqa: F811
                                                                 monkeypatch):
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_GRANT_PER_MINUTE", 5, raising=False)
    member = make_account("alex")
    browser = platform["portal"].test_client()
    login(browser, member)
    first, _ = _platform_sign_in(platform, browser)
    first.post("/platform-oauth/signup", data={"username": "alex"}, base_url=platform["url"])
    assert _signed_in(platform, first)

    assert [_bogus_callback(platform, "203.0.113.7") for _ in range(25)] == [302] * 25
    again, _location = _platform_sign_in(platform, browser)
    assert _signed_in(platform, again), "a valid code is still redeemed"
    assert _hits(query, "oauth_bad_grant") == 5, "refused codes are counted per wiki, and the count is bounded"


def test_hw2_1_a_class_signs_in_through_the_portal_at_once(platform, make_account, login):  # noqa: F811
    signed_in = 0
    for i in range(24):
        addr = f"198.51.100.{i + 1}"
        browser = platform["portal"].test_client()
        browser.environ_base["REMOTE_ADDR"] = addr
        login(browser, make_account(f"pupil{i}"))
        wiki = platform["wiki"].test_client()
        wiki.environ_base["REMOTE_ADDR"] = addr
        start = urlsplit(wiki.get("/platform-oauth/login", base_url=platform["url"]).headers["Location"])
        answer = browser.post(f"{start.path}?{start.query}", data={"action": "allow"})
        back = urlsplit(answer.headers["Location"])
        location = wiki.get(f"{back.path}?{back.query}", base_url=platform["url"]).headers.get("Location", "")
        if location.endswith("/platform-oauth/connect"):
            wiki.post("/platform-oauth/signup", data={"username": f"pupil{i}"}, base_url=platform["url"])
        signed_in += _signed_in(platform, wiki)
    assert signed_in == 24


def _basic(client_id: str, secret: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


def _client_credentials(platform) -> tuple[str, str]:  # noqa: F811
    from bananawiki.hosting import oauth
    from bananawiki.hosting.db import connection_scope, db

    with platform["portal"].test_request_context("/"), connection_scope():
        client = oauth.client_for(db.one("SELECT * FROM instances WHERE id = ?", (platform["inst"]["id"],)))
    return client.client_id, client.client_secret


def test_hw2_1_wrong_client_secrets_are_bounded_per_network(platform, monkeypatch):  # noqa: F811
    from bananawiki.hosting import limits

    monkeypatch.setattr(limits, "BAD_CLIENT_PER_MINUTE", 3)
    client_id, secret = _client_credentials(platform)
    attacker, wiki = platform["portal"].test_client(), platform["portal"].test_client()
    attacker.environ_base["REMOTE_ADDR"] = "203.0.113.9"
    form = {"grant_type": "authorization_code", "code": "x"}
    codes = [attacker.post("/oauth/token", data=form, headers=_basic(client_id, f"guess{i}")).status_code
             for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert attacker.post("/oauth/token", data=form, headers=_basic(client_id, secret)).status_code == 429, \
        "over the limit, the network's credentials are no longer checked"
    status = "/oauth/link-status?wiki_user_id=u1"
    assert attacker.get(status, headers=_basic(client_id, secret)).status_code == 429
    assert wiki.get(status, headers=_basic(client_id, secret)).status_code == 200, "the wiki itself is not refused"
    assert wiki.post("/oauth/token", data=form, headers=_basic(client_id, secret)).status_code == 400


def test_hw2_1_each_wiki_has_a_ceiling_of_authenticated_calls(platform):  # noqa: F811
    from bananawiki.hosting import limits

    portal = platform["portal"]  # noqa: F811
    portal.config["HOSTING"] = dataclasses.replace(portal.config["HOSTING"], oauth_client_rate=60)
    client_id, secret = _client_credentials(platform)
    wiki = portal.test_client()
    codes = [wiki.get("/oauth/signouts", headers=_basic(client_id, secret)).status_code for _ in range(61)]
    assert codes == [200] * 60 + [429]
    assert not limits.is_server_to_server(portal.view_functions["oauth.authorize"])


# HW2-2: the administrator's wiki download is a POST, and no download leaves its work folder behind


def _work_dirs(portal) -> list[Path]:  # noqa: F811
    root = Path(portal.config["HOSTING"].archives.export_temp_dir)
    return sorted(root.glob("bwh-archive-*")) if root.exists() else []


def _export_calls(runtime, monkeypatch) -> list[str]:  # noqa: F811
    calls: list[str] = []
    export = runtime.export_archive

    def counting(spec, destination_dir):
        calls.append(spec.slug)
        return export(spec, destination_dir)

    monkeypatch.setattr(runtime, "export_archive", counting)
    return calls


def test_hw2_2_admin_download_get_and_head_only_lead_to_the_button(portal, web, make_account,  # noqa: F811
                                                                   make_wiki, login, runtime,  # noqa: F811
                                                                   monkeypatch):
    admin = make_account("boss", admin=True)
    inst = make_wiki(make_account("owner1"), "big")
    calls = _export_calls(runtime, monkeypatch)
    login(web, admin)
    url = f"/admin/instances/{inst['id']}/download"
    for method in (web.get, web.head):
        response = method(url)
        assert response.status_code == 302 and response.headers["Location"].endswith(f"/admin/instances/{inst['id']}")
    assert calls == [] and _work_dirs(portal) == []
    page = web.get(f"/admin/instances/{inst['id']}").get_data(as_text=True)
    assert f'method="post" action="{url}"' in page

    response = web.post(url)
    assert response.status_code == 200 and response.data.startswith(b"PK") and calls == ["big"]
    assert _work_dirs(portal) == []
    for _ in range(9):
        web.post(url).close()
    assert web.post(url).status_code == 429, "the download is counted"


def test_hw2_2_a_download_closed_unread_removes_its_work_folder(portal, tmp_path):  # noqa: F811
    """What a HEAD (``HeadAsGet``) or a client gone before the first byte does: the server closes the body
    without reading it, so the generator never starts. The work folder must go all the same."""
    from werkzeug.test import EnvironBuilder

    from bananawiki.hosting.routes.dashboard import stream_file

    work = tmp_path / "bwh-archive-x"
    work.mkdir()
    (work / "a.zip").write_bytes(b"PK" * 10)
    with portal.test_request_context("/"):
        response = stream_file(work / "a.zip", "a.zip", work)
    body = response(EnvironBuilder(path="/").get_environ(), lambda *_args: None)
    body.close()
    assert not work.exists()


# HW2-3: concurrent route publications cannot publish a table read before the other's change


def test_hw2_3_a_stale_routing_table_is_never_published_last(portal, runtime, make_account, make_wiki,  # noqa: F811
                                                             query, monkeypatch):  # noqa: F811
    from bananawiki.hosting import instances
    from bananawiki.hosting.db import connection_scope

    inst = make_wiki(make_account())
    query("UPDATE instances SET status = 'running' WHERE id = ?", (inst["id"],))
    published: list[set[str]] = []
    reading, release = threading.Event(), threading.Event()

    def publish(specs):
        if not published and not reading.is_set():
            reading.set()  # the first worker read the table with the wiki running, and is slow to publish it
            release.wait(5)
        published.append({spec.instance_id for spec in specs})

    monkeypatch.setattr(runtime, "sync_routes", publish)

    def worker(change: str | None = None):
        with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
            if change:
                from bananawiki.hosting.db import db

                db.execute(change, (inst["id"],))
            instances.sync_routes()

    stale = threading.Thread(target=worker)
    stale.start()
    assert reading.wait(5)
    fresh = threading.Thread(target=worker, args=("UPDATE instances SET status = 'suspended' WHERE id = ?",))
    fresh.start()
    fresh.join(0.3)  # without the lock it publishes now, before the stale table
    release.set()
    stale.join(5)
    fresh.join(5)
    assert published[-1] == set(), "the last table published is the one read after the suspension"
