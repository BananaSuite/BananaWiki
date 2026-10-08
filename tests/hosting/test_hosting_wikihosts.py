"""Wiki hostnames that reach the portal: proxy the wiki or show its status, never the portal."""

from __future__ import annotations

import http.server
import json
import threading

import pytest


class _Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        payload = json.dumps({
            "method": self.command, "path": self.path, "host": self.headers.get("Host"),
            "forwarded_for": self.headers.get("X-Forwarded-For"), "proto": self.headers.get("X-Forwarded-Proto"),
            "forwarded_host": self.headers.get("X-Forwarded-Host"), "cookie": self.headers.get("Cookie"),
            "body": body.decode("utf-8", "replace"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "a=1; Path=/")
        self.send_header("Set-Cookie", "b=2; Path=/")
        if self.path == "/login":
            self.send_header("Set-Cookie", "__Host-bw_session_team=v1; Secure; HttpOnly; Path=/; SameSite=Lax")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def upstream():
    server = _Upstream(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def _host(slug: str) -> str:
    return f"{slug}-hosting.wiki.test"


def test_a_running_wiki_is_proxied_not_the_portal(web, runtime, make_account, make_wiki, upstream):
    owner = make_account()
    make_wiki(owner, "team")
    runtime.upstreams["team"] = upstream
    web.set_cookie("s", "abc", domain=_host("team"))
    response = web.post("/page/one?x=1", data="hello", headers={"Host": _host("team")},
                        content_type="text/plain", environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert response.status_code == 200
    seen = response.get_json()
    assert seen["method"] == "POST" and seen["path"] == "/page/one?x=1" and seen["body"] == "hello"
    assert seen["host"] == _host("team") and seen["forwarded_host"] == _host("team")
    assert seen["forwarded_for"] == "203.0.113.9" and seen["proto"] == "http"
    assert seen["cookie"] == "s=abc"
    assert response.headers.getlist("Set-Cookie") == ["a=1; Path=/", "b=2; Path=/"]
    assert "bwh_session" not in response.headers.get("Set-Cookie", "")


def test_prefixed_wiki_session_cookies_pass_through_over_https(web, runtime, make_account, make_wiki, upstream):
    # The wiki names its session __Host-bw_session_<slug> when it sees HTTPS (X-Forwarded-Proto).
    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = upstream
    web.set_cookie("__Host-bw_session_team", "v0", domain=_host("team"))
    response = web.get("/login", headers={"Host": _host("team")}, base_url=f"https://{_host('team')}")
    seen = response.get_json()
    assert seen["proto"] == "https" and seen["cookie"] == "__Host-bw_session_team=v0"
    assert "__Host-bw_session_team=v1; Secure; HttpOnly; Path=/; SameSite=Lax" in response.headers.getlist(
        "Set-Cookie")


def test_portal_pages_are_never_served_on_a_wiki_host(web, runtime, make_account, make_wiki):
    make_wiki(make_account(), "team")
    response = web.get("/", headers={"Host": _host("team")})
    assert response.status_code == 503
    assert "The wiki is starting" in response.get_data(as_text=True)
    assert "Location" not in response.headers


def test_paused_and_unknown_wikis_get_a_status_page(web, runtime, make_account, make_wiki, query):
    inst = make_wiki(make_account(), "team")
    query("UPDATE instances SET status = 'stopped' WHERE id = ?", (inst["id"],))
    paused = web.get("/login", headers={"Host": _host("team")})
    assert paused.status_code == 503 and "paused" in paused.get_data(as_text=True)
    missing = web.get("/", headers={"Host": _host("nobody")})
    assert missing.status_code == 404 and "No wiki at this address" in missing.get_data(as_text=True)


def test_suspension_details_are_shown_only_when_visible(web, make_account, make_wiki, query):
    inst = make_wiki(make_account(), "team")
    query("UPDATE instances SET status = 'suspended', suspend_reason = 'spam', suspend_reason_visible = 1 "
          "WHERE id = ?", (inst["id"],))
    response = web.get("/", headers={"Host": _host("team")})
    assert response.status_code == 403 and "spam" in response.get_data(as_text=True)
    query("UPDATE instances SET suspend_reason_visible = 0 WHERE id = ?", (inst["id"],))
    assert "spam" not in web.get("/", headers={"Host": _host("team")}).get_data(as_text=True)


def test_portal_hosts_still_reach_the_portal(web):
    for host in ("localhost", "wiki.test", "www.wiki.test", "hosting.wiki.test", "127.0.0.1:5099"):
        response = web.get("/", headers={"Host": host})
        assert response.status_code == 302, host
        assert response.headers["Location"].endswith("/login"), host


def test_an_unreachable_wiki_gets_a_retry_page(web, runtime, make_account, make_wiki):
    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = ("127.0.0.1", 1)
    response = web.get("/", headers={"Host": _host("team")})
    # 503, never 502/504: Cloudflare would replace the page with its own "Bad gateway".
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert 'http-equiv="refresh"' in response.get_data(as_text=True)


def test_wiki_status_pages_are_never_502_or_504(web):
    from bananawiki.hosting import wikihosts

    app = web.application
    middleware = app.wsgi_app
    while not isinstance(middleware, wikihosts.WikiHosts):
        middleware = middleware.app if hasattr(middleware, "app") else middleware.wsgi_app
    with app.test_request_context("/"):
        for state in ("timeout", "bad_gateway", "busy", "starting"):
            status, headers, _body = middleware._page(state, "team-hosting.wiki.test")
            assert status.startswith("503 "), state
            assert ("Retry-After", "5") in headers


def test_large_bodies_are_streamed_to_the_wiki(web, runtime, make_account, make_wiki, upstream):
    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = upstream
    body = "x" * 300_000
    response = web.post("/upload", data=body, headers={"Host": _host("team")}, content_type="text/plain")
    assert response.status_code == 200 and response.get_json()["body"] == body


def test_head_requests_have_no_body(web, runtime, make_account, make_wiki, upstream):
    make_wiki(make_account(), "team")
    runtime.upstreams["team"] = upstream
    response = web.head("/", headers={"Host": _host("team")})
    assert response.status_code in (200, 501) and response.get_data() == b""
