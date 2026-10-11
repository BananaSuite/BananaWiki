"""Per-address limits outside sign-in count an IPv6 client by its /64 (``core.web.client_network``).

One IPv6 subscriber usually holds a whole /64; keyed on the full address,
each anonymous limit below would be unlimited for them.
"""

from __future__ import annotations

import pytest
from flask import g

from bananawiki.wiki.db import connection_scope

from .chat_support import JSON

NETWORK = "2001:db8:1:2::/64"


class _Spy:
    def __init__(self):
        self.keys: list[str] = []

    def hit(self, key: str, *_args) -> bool:
        self.keys.append(key)
        return True


def _pages():
    from bananawiki.wiki.features.pages.blueprint import rate_limited

    rate_limited("probe", 5)(lambda: "ok")()


def _canvas():
    from bananawiki.wiki.features.canvas.routes import _limit

    _limit("probe", 5)


def _tts():
    from bananawiki.wiki.features.tts.routes import _rate_limited

    _rate_limited("probe", 5, 60)


def _chat():
    from bananawiki.wiki.features.chat.web import throttle

    throttle("probe", 5)


@pytest.mark.parametrize("limit", [_pages, _canvas, _tts, _chat], ids=["pages", "canvas", "tts", "chat"])
def test_anonymous_limits_count_an_ipv6_client_by_its_64(app, monkeypatch, limit):
    spy = _Spy()
    monkeypatch.setitem(app.extensions, "bananawiki.limiter", spy)
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "2001:db8:1:2::5"}), connection_scope():
        g.user = None
        limit()
    assert spy.keys and all(key.endswith(NETWORK) for key in spy.keys), spy.keys


def test_group_joins_count_an_ipv6_client_by_its_64(app, make_user, login, monkeypatch):
    from bananawiki.wiki.features.chat import routes_groups

    monkeypatch.setattr(routes_groups, "JOIN_MAX_PER_IP", 2)
    statuses = []
    for index in range(3):
        client = app.test_client()
        login(client, make_user(f"joiner{index}"))
        statuses.append(client.post("/groups/join", data={"invite_code": "nope"}, headers=JSON,
                                    environ_base={"REMOTE_ADDR": f"2001:db8:1:2::{index + 1:x}"}).status_code)
    assert statuses == [404, 404, 429]
