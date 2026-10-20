"""Wiki lifetimes: no expiry when configured, warnings before expiry, a notice
at expiry and renewal by owners and collaborators allowed to start and stop
the wiki, when the platform allows it."""

from __future__ import annotations

from datetime import timedelta

from bananawiki.core.timeutil import parse, to_sql, utcnow
from bananawiki.hosting import email
from bananawiki.hosting.db import connection_scope

from .hosting_support import PASSWORD, build_portal, portal_environ


def _scope(portal):
    return connection_scope(portal.extensions["bananawiki.hosting.database"])


def _setup(portal, *, owner_email: str = "owner@example.org"):
    with portal.test_request_context("/"), _scope(portal):
        from bananawiki.hosting import accounts, instances

        owner = accounts.create("owner", PASSWORD, email=owner_email)
        return owner, instances.create(owner, "lifetime")[0]


def _expires_in(portal, wiki, delta: timedelta) -> None:
    with portal.app_context(), _scope(portal) as db:
        db.execute("UPDATE instances SET expires_at = ? WHERE id = ?", (to_sql(utcnow() + delta), wiki["id"]))


def _warn(portal) -> list:
    with portal.test_request_context("/"), _scope(portal), email.capture_outbox() as outbox:
        from bananawiki.hosting import instances

        instances.warn_expiring()
    return outbox


def test_duration_zero_means_wikis_never_expire(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, INSTANCE_DURATION_DAYS="0"))
    _owner, wiki = _setup(portal)
    assert wiki["expires_at"] is None


def test_owners_are_warned_once_at_each_threshold_before_expiry(portal):
    _owner, wiki = _setup(portal)
    _expires_in(portal, wiki, timedelta(days=5))
    assert _warn(portal) == []
    _expires_in(portal, wiki, timedelta(days=2, hours=23))
    first = _warn(portal)
    assert [message.to for message in first] == ["owner@example.org"]
    assert "lifetime" in first[0].subject
    assert _warn(portal) == [], "each warning goes out once"
    with portal.app_context(), _scope(portal) as db:
        expires = db.scalar("SELECT expires_at FROM instances WHERE id = ?", (wiki["id"],))
        db.execute("UPDATE instances SET expires_at = ? WHERE id = ?",
                   (to_sql(parse(expires) - timedelta(days=2)), wiki["id"]))
    assert len(_warn(portal)) == 1, "the last day gets its own warning"
    assert _warn(portal) == []
    _expires_in(portal, wiki, timedelta(days=2))
    assert len(_warn(portal)) == 1, "a changed expiry date is announced again"


def test_expired_wikis_are_announced_to_their_owner(portal):
    _owner, wiki = _setup(portal)
    _expires_in(portal, wiki, timedelta(minutes=-1))
    with portal.test_request_context("/"), _scope(portal), email.capture_outbox() as outbox:
        from bananawiki.hosting import instances

        assert instances.terminate_expired() == 1
    assert [message.to for message in outbox] == ["owner@example.org"]
    assert "lifetime" in outbox[0].subject


def test_owners_renew_only_when_the_platform_allows_it(portal, query):
    owner, wiki = _setup(portal)
    client = portal.test_client()
    client.post("/login", data={"username": "owner", "password": PASSWORD})
    _expires_in(portal, wiki, timedelta(days=2))
    before = query("SELECT expires_at FROM instances", one=True)["expires_at"]
    assert "/renew" not in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    client.post(f"/instances/{wiki['id']}/renew")
    assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == before
    query("UPDATE hosting_settings SET owner_renewal_enabled = 1 WHERE id = 1")
    assert "/renew" in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    client.post(f"/instances/{wiki['id']}/renew")
    renewed = parse(query("SELECT expires_at FROM instances", one=True)["expires_at"])
    assert abs(renewed - (utcnow() + timedelta(days=14))) < timedelta(minutes=5)
    client.post(f"/instances/{wiki['id']}/renew")
    assert parse(query("SELECT expires_at FROM instances", one=True)["expires_at"]) == renewed, \
        "a wiki far from its expiry is not renewed again"


def test_a_warning_that_could_not_be_sent_is_tried_again(portal, monkeypatch):
    from bananawiki.hosting import notifications

    _owner, wiki = _setup(portal)
    _expires_in(portal, wiki, timedelta(days=1))
    real_send = notifications.send
    monkeypatch.setattr(notifications, "send", lambda *args, **kwargs: (False, "daily_limit"))
    assert _warn(portal) == []
    monkeypatch.setattr(notifications, "send", real_send)
    assert len(_warn(portal)) == 1
    assert _warn(portal) == []


def _renewing_portal(portal, query):
    owner, wiki = _setup(portal)
    query("UPDATE hosting_settings SET owner_renewal_enabled = 1 WHERE id = 1")
    _expires_in(portal, wiki, timedelta(days=2))
    return owner, wiki


def _signed_in(portal, username):
    client = portal.test_client()
    assert client.post("/login", data={"username": username, "password": PASSWORD}).status_code == 302
    return client


def _collaborator(portal, owner, wiki, username, permissions):
    with portal.test_request_context("/"), _scope(portal):
        from bananawiki.hosting import accounts, collaborators, instances

        helper = accounts.create(username, PASSWORD)
        collaborators.add(instances.get(wiki["id"]), owner, username, "custom", permissions)
    return helper


def test_a_collaborator_who_may_start_and_stop_the_wiki_renews_it(portal, query):
    owner, wiki = _renewing_portal(portal, query)
    helper = _collaborator(portal, owner, wiki, "helper", ["start_stop"])
    client = _signed_in(portal, "helper")
    assert "/renew" in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert client.post(f"/instances/{wiki['id']}/renew").status_code == 302
    renewed = parse(query("SELECT expires_at FROM instances", one=True)["expires_at"])
    assert abs(renewed - (utcnow() + timedelta(days=14))) < timedelta(minutes=5)
    event = query("SELECT actor_id FROM hosting_events WHERE action = 'instance.renewed'", one=True)
    assert event["actor_id"] == helper["id"]


def test_a_collaborator_without_start_and_stop_does_not_renew(portal, query):
    owner, wiki = _renewing_portal(portal, query)
    _collaborator(portal, owner, wiki, "viewer", ["download"])
    before = query("SELECT expires_at FROM instances", one=True)["expires_at"]
    client = _signed_in(portal, "viewer")
    assert "/renew" not in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert client.post(f"/instances/{wiki['id']}/renew").status_code == 403
    assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == before
    assert query("SELECT 1 FROM hosting_events WHERE action = 'instance.renewed'", one=True) is None


def test_collaborators_renew_only_when_the_platform_lets_owners_renew(portal, query):
    owner, wiki = _renewing_portal(portal, query)
    _collaborator(portal, owner, wiki, "helper", ["start_stop"])
    query("UPDATE hosting_settings SET owner_renewal_enabled = 0 WHERE id = 1")
    before = query("SELECT expires_at FROM instances", one=True)["expires_at"]
    for username in ("helper", "owner"):
        client = _signed_in(portal, username)
        assert "/renew" not in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
        client.post(f"/instances/{wiki['id']}/renew")
        assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == before


def test_an_end_date_an_administrator_brought_forward_is_not_renewed_away(portal, query):
    owner, wiki = _renewing_portal(portal, query)
    with portal.test_request_context("/"), _scope(portal):
        from bananawiki.hosting import accounts

        accounts.create("moderator", PASSWORD, is_admin=True)
    moderator = _signed_in(portal, "moderator")
    assert moderator.post(f"/admin/instances/{wiki['id']}/shorten-expiry",
                          data={"shorten_days": "1"}).status_code == 302
    shortened = query("SELECT expires_at FROM instances", one=True)["expires_at"]
    client = _signed_in(portal, "owner")
    assert "/renew" not in client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    client.post(f"/instances/{wiki['id']}/renew")
    assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == shortened
    outbox = _warn(portal)
    assert outbox and "renew it" not in outbox[0].text, "the warning does not offer a renewal it cannot give"
    # Extending it again gives the decision back to the owner.
    moderator.post(f"/admin/instances/{wiki['id']}/extend", data={"extra_hours": "1"})
    client.post(f"/instances/{wiki['id']}/renew")
    renewed = parse(query("SELECT expires_at FROM instances", one=True)["expires_at"])
    assert abs(renewed - (utcnow() + timedelta(days=14))) < timedelta(minutes=5)
