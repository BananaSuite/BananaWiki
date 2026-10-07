"""Timestamps at the ends of datetime's range can no longer break the pages that show them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from bananawiki.core import timeutil
from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import templating
from bananawiki.wiki.features.api_service import tokens
from tests.features.api_support import call, enable_api, issue
from tests.features.pages_support import in_app

FOREVER = "9999-12-31 23:59:59"
PLUS_ONE = timezone(timedelta(hours=1))
MINUS_ONE = timezone(timedelta(hours=-1))
EAST_MOST = timezone(timedelta(hours=23, minutes=59))
WEST_MOST = timezone(-timedelta(hours=23, minutes=59))


@pytest.fixture
def rome(app, db):
    db.execute("UPDATE site_settings SET timezone = 'Europe/Rome' WHERE id = 1")
    return app


@pytest.fixture
def api_app(rome, db):
    enable_api(rome, db)
    return rome


# ── timeutil ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [
    FOREVER, "9999-12-31T23:59:59-01:00", "0001-01-01 00:00:00", "0001-01-01T00:00:00+01:00",
    "1899-12-31 23:59:59", datetime(9999, 12, 31, tzinfo=UTC),
])
def test_bounded_parse_refuses_years_outside_the_safe_range(value):
    assert timeutil.parse(value, bounded=True) is None
    assert timeutil.parse(value) is not None  # stored values are still read as they are


@pytest.mark.parametrize("value", [1e20, -1e20, float("nan")])
def test_parse_refuses_numbers_outside_datetime(value):
    assert timeutil.parse(value) is None


@pytest.mark.parametrize("value", [
    "9998-12-31T23:59:59", "9998-12-31T23:59:59+05:00", "1900-01-01T00:00:00", "1900-01-01T00:00:00-05:00",
    "2026-10-04 12:00:00",
])
def test_valid_moments_round_trip_and_show_in_any_time_zone(value):
    moment = timeutil.parse(value, bounded=True)
    assert moment is not None
    assert timeutil.parse(timeutil.to_sql(moment), bounded=True) == moment
    for zone in (EAST_MOST, WEST_MOST):
        assert moment.astimezone(zone) == moment


@pytest.mark.parametrize("value", ["9998-12-31T23:59:59-01:00", "1900-01-01T00:30:00+01:00"])
def test_the_range_is_checked_in_utc(value):
    """Stored in UTC these fall in 9999 and 1899, so an accepted value is never refused when read back."""
    assert timeutil.parse(value, bounded=True) is None
    assert timeutil.parse(value) is not None


def test_far_dates_still_compare():
    """Chat stores indefinite timeouts as 9999-12-31; they must stay in the future."""
    assert timeutil.parse(FOREVER) == datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert timeutil.is_future(FOREVER) and not timeutil.is_past(FOREVER)
    assert timeutil.is_past("0001-01-01T00:00:00+01:00")
    assert not timeutil.is_future("garbage") and not timeutil.is_past("garbage")


def test_to_sql_and_in_zone_never_overflow():
    assert timeutil.to_sql(datetime(9999, 12, 31, 23, 59, tzinfo=MINUS_ONE)) == "9999-12-31 23:59:00"
    assert timeutil.to_sql(datetime(1, 1, 1, tzinfo=PLUS_ONE)) == "0001-01-01 00:00:00"
    assert timeutil.to_sql(datetime(2026, 1, 2, 3, 4, 5, 678, tzinfo=PLUS_ONE)) == "2026-01-02 02:04:05"
    last = datetime(9999, 12, 31, 23, 30, tzinfo=UTC)
    assert timeutil.in_zone(last, PLUS_ONE).utcoffset() == timedelta(0)  # shown in UTC instead
    assert timeutil.in_zone(datetime(9999, 12, 31, 12, tzinfo=UTC), PLUS_ONE).hour == 13


# ── Template filters ──────────────────────────────────────────────────────────


def test_filters_show_far_dates_in_the_site_time_zone_or_utc(rome):
    def render():
        return (templating.format_datetime("9999-12-31 23:30:00"), templating.format_datetime("2026-07-01 10:00:00"),
                templating.time_ago(FOREVER), templating.time_ago("0001-01-01T00:00:00+01:00"),
                templating.datetime_local_input("9999-12-31 22:00:00"))

    far, summer, future, ancient, local = in_app(rome, render)
    assert far == "9999-12-31 23:30" and summer == "2026-07-01 12:00"
    assert future == "in the future" and "years ago" in ancient
    assert local == "9999-12-31T23:00"


def test_portal_filters_show_far_dates():
    """The hosting portal shows stored dates in UTC: a wiki expiring in 9999 still shows its expiry."""
    from bananawiki.hosting import templating as portal

    assert portal.format_datetime(FOREVER) == "9999-12-31 23:59 UTC"


def test_local_inputs_outside_the_range_are_invalid(rome):
    def read(text: str) -> str | None:
        return timeutil.to_sql(in_app(rome, lambda: templating.from_local_input(text)))

    assert read("9999-12-31T23:59") is None and read("0001-01-01T00:30") is None
    assert read("1900-01-01T00:30") is None  # 1899 in UTC
    assert read("2030-05-01T10:00") == "2030-05-01 08:00:00"


# ── API tokens (the reported case) ────────────────────────────────────────────


def test_token_expiring_in_9999_keeps_the_token_pages_working(api_app, client, db, login, admin):
    """A 9999-12-31 expiry turned /admin/api-service and /settings/api-tokens into a 400 for good."""
    raw = issue(api_app, admin, ["tokens", "pages"], expires_at=FOREVER)
    login(client, admin)
    admin_page = client.get("/admin/api-service")
    assert admin_page.status_code == 200 and "9999-12-31 23:59" in admin_page.get_data(as_text=True)
    own_page = client.get("/settings/api-tokens")
    assert own_page.status_code == 200 and "9999-12-31 23:59" in own_page.get_data(as_text=True)
    assert call(client, "GET", "/pages", raw).status_code == 200  # an existing far expiry still works
    listed = call(client, "GET", "/tokens", raw).json["tokens"]
    assert [token["expires_at"] for token in listed] == ["9999-12-31T23:59:59Z"]  # not shown as permanent
    token_id = db.scalar("SELECT id FROM api_service__tokens")
    client.post(f"/settings/api-tokens/{token_id}/revoke")
    assert db.scalar("SELECT active FROM api_service__tokens WHERE id = ?", (token_id,)) == 0
    assert call(client, "GET", "/pages", raw).status_code == 401


@pytest.mark.parametrize("expires_at", [
    "9999-12-31T23:59:59", "9999-12-31T23:59:59-05:00", pytest.param(None, id="ten_years_and_a_day"),
])
def test_api_refuses_expiries_more_than_ten_years_ahead(api_app, client, admin, expires_at):
    # Computed here: a parameter that depends on the clock would differ between xdist workers.
    expires_at = expires_at or sql_in(days=366 * 10 + 1).replace(" ", "T") + "Z"
    parent = issue(api_app, admin, ["tokens", "pages"])
    response = call(client, "POST", "/tokens", parent, json={"expires_at": expires_at,
                                                             "permissions": {"read": True, "scopes": ["pages"]}})
    assert response.status_code == 400 and response.json["code"] == "expiry_too_far"
    assert "10 years" in response.json["error"]


def test_api_accepts_expiries_within_ten_years_and_refuses_ancient_ones(api_app, client, admin):
    parent = issue(api_app, admin, ["tokens", "pages"])
    grant = {"read": True, "scopes": ["pages"]}
    within = call(client, "POST", "/tokens", parent,
                  json={"expires_at": sql_in(days=365 * 9).replace(" ", "T") + "Z", "permissions": grant})
    assert within.status_code == 201
    ancient = call(client, "POST", "/tokens", parent,
                   json={"expires_at": "0001-01-01T00:00:00+01:00", "permissions": grant})
    assert ancient.json["code"] == "expiry_in_past"


def test_children_of_a_far_expiring_token_can_still_choose_an_expiry(api_app, client, admin):
    parent = issue(api_app, admin, ["tokens", "pages"], expires_at=FOREVER)
    child = call(client, "POST", "/tokens", parent, json={"expires_at": sql_in(days=30).replace(" ", "T") + "Z",
                                                          "permissions": {"read": True, "scopes": ["pages"]}})
    assert child.status_code == 201


def test_token_page_refuses_expiries_more_than_ten_years_ahead(api_app, client, db, login, admin):
    login(client, admin)
    for value in ("9999-12-31T23:59", (datetime.now(UTC) + timedelta(days=366 * 11)).strftime("%Y-%m-%dT%H:%M")):
        page = client.post("/settings/api-tokens/create", data={"name": "far", "expires_at": value},
                           follow_redirects=True)
        assert page.status_code == 200
    assert "10 years" in page.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 0
    soon = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
    assert client.post("/settings/api-tokens/create", data={"name": "soon", "expires_at": soon}).status_code == 201
    assert db.scalar("SELECT COUNT(*) FROM api_service__tokens") == 1
    assert tokens.MAX_EXPIRY_YEARS == 10


# ── Chat ──────────────────────────────────────────────────────────────────────


def test_chat_polling_ignores_a_since_cursor_outside_the_range(client, login, make_user):
    from tests.features.chat_support import JSON, start_dm

    login(client, make_user("alice"))
    make_user("bob")
    dm = start_dm(client, "bob")
    for since in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
        response = client.get(f"/chats/{dm}/messages", query_string={"after": 0, "since": since}, headers=JSON)
        assert response.status_code == 200 and response.json["deleted"] == []
