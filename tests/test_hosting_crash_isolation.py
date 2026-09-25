"""Tests for the per-instance circuit breaker and concurrency cap (task #8).

A single wedged BananaWiki instance must NOT be able to take down the
hosting portal or slow down its siblings.  These tests pin down the
two safeguards that enforce that:

* The **circuit breaker** flips from CLOSED → OPEN after
  ``_CIRCUIT_BREAKER_FAILURE_THRESHOLD`` consecutive upstream failures
  and fast-fails subsequent requests (no 120 s wait) until the
  cooldown elapses.
* The **per-instance concurrency cap** uses a bounded semaphore so an
  instance under heavy slow-traffic cannot starve portal workers
  used by every *other* instance.

Both pieces are pure Python and easy to verify without spinning a
real BananaWiki instance up.
"""

from __future__ import annotations

import io

import pytest


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """Clear breaker + semaphore state before and after every test."""
    from hosting import _subdomain_proxy

    _subdomain_proxy._reset_circuit_breaker_state_for_tests()
    yield
    _subdomain_proxy._reset_circuit_breaker_state_for_tests()


class TestCircuitBreaker:
    """The breaker MUST open after repeated failures and recover after cooldown."""

    def test_breaker_closed_by_default(self):
        from hosting._subdomain_proxy import _circuit_is_open

        assert _circuit_is_open("wiki", "hosting") is False

    def test_breaker_opens_after_threshold(self):
        from hosting import _subdomain_proxy

        threshold = _subdomain_proxy._CIRCUIT_BREAKER_FAILURE_THRESHOLD
        for _ in range(threshold):
            _subdomain_proxy._record_upstream_failure("wiki", "hosting", "test")

        assert _subdomain_proxy._circuit_is_open("wiki", "hosting") is True

    def test_breaker_isolated_per_instance(self):
        """One failing instance must not open the breaker for any other."""
        from hosting import _subdomain_proxy

        threshold = _subdomain_proxy._CIRCUIT_BREAKER_FAILURE_THRESHOLD
        for _ in range(threshold):
            _subdomain_proxy._record_upstream_failure("brokenwiki", "hosting", "test")

        assert _subdomain_proxy._circuit_is_open("brokenwiki", "hosting") is True
        assert _subdomain_proxy._circuit_is_open("healthywiki", "hosting") is False

    def test_breaker_isolates_apex_from_hosting(self):
        """The (subdomain, domain_mode) key must isolate apex vs hosting."""
        from hosting import _subdomain_proxy

        threshold = _subdomain_proxy._CIRCUIT_BREAKER_FAILURE_THRESHOLD
        for _ in range(threshold):
            _subdomain_proxy._record_upstream_failure("wiki", "apex", "test")

        assert _subdomain_proxy._circuit_is_open("wiki", "apex") is True
        assert _subdomain_proxy._circuit_is_open("wiki", "hosting") is False

    def test_breaker_closes_on_success(self):
        """Recording a success must reset the failure counter."""
        from hosting import _subdomain_proxy

        threshold = _subdomain_proxy._CIRCUIT_BREAKER_FAILURE_THRESHOLD
        for _ in range(threshold - 1):
            _subdomain_proxy._record_upstream_failure("wiki", "hosting", "test")
        _subdomain_proxy._record_upstream_success("wiki", "hosting")

        # The next failure should NOT immediately re-trip the breaker.
        _subdomain_proxy._record_upstream_failure("wiki", "hosting", "test")
        assert _subdomain_proxy._circuit_is_open("wiki", "hosting") is False

    def test_breaker_half_opens_after_cooldown(self, monkeypatch):
        """After the cooldown, one probe is allowed through (half-open)."""
        from hosting import _subdomain_proxy

        # Shrink the cooldown for the test.
        monkeypatch.setattr(
            _subdomain_proxy, "_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0.0
        )

        threshold = _subdomain_proxy._CIRCUIT_BREAKER_FAILURE_THRESHOLD
        for _ in range(threshold):
            _subdomain_proxy._record_upstream_failure("wiki", "hosting", "test")

        assert _subdomain_proxy._circuit_is_open("wiki", "hosting") is False


class TestConcurrencyCap:
    """The per-instance semaphore caps in-flight requests."""

    def test_semaphore_created_per_instance(self):
        from hosting import _subdomain_proxy

        a = _subdomain_proxy._get_instance_semaphore("wiki", "hosting")
        b = _subdomain_proxy._get_instance_semaphore("other", "hosting")
        c = _subdomain_proxy._get_instance_semaphore("wiki", "hosting")

        assert a is c
        assert a is not b

    def test_acquire_release_round_trip(self):
        from hosting import _subdomain_proxy

        cap = _subdomain_proxy._INSTANCE_CONCURRENCY_CAP
        sema = _subdomain_proxy._get_instance_semaphore("wiki", "hosting")

        # Drain the semaphore.
        for _ in range(cap):
            assert sema.acquire(blocking=False) is True

        # Next acquire must fail-fast.
        assert sema.acquire(blocking=False) is False

        # Releasing one slot lets the next acquire succeed.
        sema.release()
        assert sema.acquire(blocking=False) is True


class TestProxyRequestRecordsFailures:
    """The high-level ``_proxy_request`` helper must wire failures to the breaker."""

    def _wsgi_environ(self):
        return {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "HTTP_HOST": "wiki-hosting.example.com",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.url_scheme": "http",
            "REMOTE_ADDR": "127.0.0.1",
        }

    def test_connection_refused_records_failure(self, monkeypatch):
        from hosting import _subdomain_proxy

        class _BadConn:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                raise ConnectionRefusedError("nope")

            def getresponse(self):  # pragma: no cover - never reached
                raise AssertionError

            def close(self):
                pass

        monkeypatch.setattr(
            _subdomain_proxy.http.client, "HTTPConnection", _BadConn
        )

        # Single proxy attempt records exactly one failure.
        _subdomain_proxy._proxy_request(
            self._wsgi_environ(),
            port=6001,
            subdomain="wiki",
            domain_mode="hosting",
        )
        with _subdomain_proxy._circuit_breaker_lock:
            state = _subdomain_proxy._circuit_breaker_state[("wiki", "hosting")]
        assert state["failures"] == 1
