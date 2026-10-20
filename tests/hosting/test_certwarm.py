"""Certificate warm-up for new wiki hosts, and the route result that sync_routes reports."""

from __future__ import annotations

import dataclasses
import logging
import ssl
import threading

import pytest

from bananawiki.hosting import certwarm, instances
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime import DomainCheck

from .hosting_support import build_portal, portal_environ

LOG = "bananawiki.hosting.certwarm"


class Connector:
    """A stand-in for the TLS handshake: fails *failures* times, then works (or never works)."""

    def __init__(self, failures: int = 0, error: Exception | None = None):
        self.failures, self.error = failures, error
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, timeout: float) -> None:
        self.calls.append((host, port, timeout))
        if self.error is not None:
            raise self.error
        if len(self.calls) <= self.failures:
            raise ssl.SSLError("tlsv1 alert internal error")


@pytest.fixture
def connector(monkeypatch):
    monkeypatch.setattr(certwarm, "RETRY_DELAY", 0.01)
    monkeypatch.setattr(certwarm, "GIVE_UP_AFTER", 0.2)
    fake = Connector()
    monkeypatch.setattr(certwarm, "connect", fake)
    return fake


@pytest.fixture
def serving(portal):
    """The portal as it runs in production: it serves HTTPS itself and warms certificates."""
    portal.config["HOSTING"] = dataclasses.replace(portal.config["HOSTING"], testing=False)
    return portal


def schedule(portal, host: str) -> threading.Thread | None:
    with portal.app_context():
        return certwarm.schedule(host)


def test_a_slow_issuance_is_waited_for_and_reported_once(connector, caplog):
    connector.failures = 2
    with caplog.at_level(logging.INFO, logger=LOG):
        assert certwarm.warm("docs-hosting.wiki.test", 8443)
    assert connector.calls == [("docs-hosting.wiki.test", 8443, certwarm.ATTEMPT_TIMEOUT)] * 3
    assert [record.getMessage() for record in caplog.records] == [
        "Certificate for docs-hosting.wiki.test is ready."]


def test_giving_up_logs_one_warning_without_the_error_text(connector, caplog):
    connector.error = ConnectionRefusedError("password=hunter2")
    with caplog.at_level(logging.INFO, logger=LOG):
        assert not certwarm.warm("docs-hosting.wiki.test")
    assert len(connector.calls) > 1, "it retried"
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "docs-hosting.wiki.test" in record.getMessage() and "ConnectionRefusedError" in record.getMessage()
    assert "hunter2" not in record.getMessage()


def test_retries_back_off_so_a_failing_order_is_not_repeated_every_few_seconds(connector, monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(certwarm.time, "sleep", waits.append)
    connector.error = ssl.SSLError("tlsv1 alert internal error")
    assert not certwarm.warm("docs-hosting.wiki.test")
    assert len(waits) >= 3
    assert waits == [certwarm.RETRY_DELAY * 2**step for step in range(len(waits))]
    assert len(connector.calls) == len(waits) + 1


def test_the_default_handshake_targets_loopback_without_verifying(monkeypatch):
    seen = {}

    class Plain:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Context:
        def wrap_socket(self, plain, server_hostname):
            seen["server_hostname"] = server_hostname
            return Plain()

    def create_context():
        context = Context()
        seen["context"] = context
        return context

    monkeypatch.setattr(certwarm.socket, "create_connection",
                        lambda address, timeout: seen.update(address=address, timeout=timeout) or Plain())
    monkeypatch.setattr(certwarm.ssl, "create_default_context", create_context)
    certwarm.handshake("docs-hosting.wiki.test", 443, 100)
    assert seen["address"] == ("127.0.0.1", 443) and seen["timeout"] == 100
    assert seen["server_hostname"] == "docs-hosting.wiki.test"
    assert seen["context"].verify_mode == ssl.CERT_NONE and seen["context"].check_hostname is False


def test_scheduling_runs_in_the_background_and_never_in_tests_by_default(portal, connector):
    assert schedule(portal, "docs-hosting.wiki.test") is None
    assert connector.calls == []


def test_scheduling_warms_the_host_in_a_daemon_thread(serving, connector, caplog):
    with caplog.at_level(logging.INFO, logger=LOG):
        thread = schedule(serving, " Docs-Hosting.WIKI.test ")
        assert thread is not None and thread.daemon
        thread.join(5)
    assert connector.calls == [("docs-hosting.wiki.test", 443, certwarm.ATTEMPT_TIMEOUT)]
    assert "Certificate for docs-hosting.wiki.test is ready." in caplog.text


def test_concurrent_warm_ups_of_a_host_are_one(serving, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocked(host, port, timeout):
        calls.append(host)
        started.set()
        release.wait(5)

    monkeypatch.setattr(certwarm, "connect", blocked)
    first = schedule(serving, "docs-hosting.wiki.test")
    assert started.wait(5)
    assert schedule(serving, "docs-hosting.wiki.test") is None, "already warming"
    other = schedule(serving, "more-hosting.wiki.test")
    assert other is not None, "another host is its own warm-up"
    release.set()
    first.join(5)
    other.join(5)
    assert sorted(set(calls)) == ["docs-hosting.wiki.test", "more-hosting.wiki.test"] and len(calls) == 2
    again = schedule(serving, "docs-hosting.wiki.test")
    assert again is not None, "a finished warm-up can run again"
    again.join(5)


def test_a_connector_that_raises_does_not_reach_the_caller(serving, connector, caplog):
    connector.error = RuntimeError("boom")
    with caplog.at_level(logging.INFO, logger=LOG):
        thread = schedule(serving, "docs-hosting.wiki.test")
        thread.join(5)
    assert "No certificate for docs-hosting.wiki.test" in caplog.text and "RuntimeError" in caplog.text
    assert "boom" not in caplog.text


def test_only_a_platform_serving_https_warms(tmp_path, connector):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_MODE="port", BASE_DOMAIN=""))
    portal.config["HOSTING"] = dataclasses.replace(portal.config["HOSTING"], testing=False)
    assert portal.config["HOSTING"].hosting_mode == "port"
    assert schedule(portal, "docs-hosting.wiki.test") is None
    assert schedule(portal, "") is None
    assert connector.calls == []


# Where the portal starts it


def test_a_new_wiki_is_warmed_once_its_routes_are_published(portal, runtime, make_account, make_wiki, monkeypatch):
    events: list[str] = []
    runtime.sync_routes = lambda specs: events.append("routes")  # type: ignore[method-assign]
    monkeypatch.setattr(certwarm, "schedule", lambda host: events.append(host))
    make_wiki(make_account(), "fresh")
    assert events == ["routes", "fresh-hosting.wiki.test"]


def test_a_verified_custom_domain_is_warmed_once(tmp_path, monkeypatch):
    app = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test"))
    runtime = app.extensions["bananawiki.hosting.runtime"]
    warmed: list[str] = []
    monkeypatch.setattr(certwarm, "schedule", warmed.append)
    with app.test_request_context("/"), connection_scope() as session:
        from bananawiki.hosting import accounts, domains

        owner = accounts.create("domainer", "correct horse 42")
        wiki = instances.create(owner, "branded")[0]
        warmed.clear()
        session.execute("UPDATE instances SET custom_domain_allowed = 1")
        domains.claim(wiki, "docs.example.org", owner["id"])
        runtime.domain_results["docs.example.org"] = DomainCheck(ownership=True, routing=True, dns_error=False)
        assert warmed == [], "nothing before the proof"
        domains.verify(wiki, owner["id"])
        assert warmed == ["docs.example.org"]
        domains.verify(wiki, None)  # the maintenance loop renews it: the certificate is long there
        assert warmed == ["docs.example.org"]


# Route publication


def test_routes_written_but_not_loaded_are_reported(ctx, runtime, caplog):
    runtime.sync_routes = lambda specs: {"routes": 1, "changed": True, "reloaded": False}  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="bananawiki.hosting.instances"):
        instances.sync_routes()
    assert [record.getMessage() for record in caplog.records] == [
        "Tenant routes were written but Caddy did not reload; new wikis stay unrouted until it does."]


@pytest.mark.parametrize("result", [None, {"routes": 1, "changed": True, "reloaded": True},
                                    {"routes": 1, "changed": False, "reloaded": False}])
def test_routes_that_loaded_or_did_not_change_are_not_reported(ctx, runtime, caplog, result):
    runtime.sync_routes = lambda specs: result  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="bananawiki.hosting.instances"):
        assert instances.sync_routes() is None
    assert caplog.records == []
