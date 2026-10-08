"""Far dates: the portal accepts the years 1900-9998, and an expiry stored in 9999 keeps working."""

from __future__ import annotations

FAR = "9999-12-31 00:00:00"


def _expiry(query) -> str:
    return query("SELECT expires_at FROM instances", one=True)["expires_at"]


def test_dates_after_the_supported_years_are_refused(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "bounded")
    login(web, admin)
    before = _expiry(query)
    page = web.post(f"/admin/instances/{wiki['id']}/set-expiry", data={"expiry_datetime": "9999-12-31T00:00"},
                    follow_redirects=True)
    assert page.status_code == 200 and "between the years 1900 and 9998" in page.get_data(as_text=True)
    assert _expiry(query) == before
    web.post(f"/admin/instances/{wiki['id']}/suspend", data={
        "suspend_duration": "custom_datetime", "suspend_custom_datetime": "9999-06-01T00:00"})
    assert query("SELECT status FROM instances", one=True)["status"] == "running"
    web.post(f"/admin/instances/{wiki['id']}/set-expiry", data={"expiry_datetime": "9998-12-31T00:00"})
    assert _expiry(query) == "9998-12-31 00:00:00"


def test_extending_a_far_expiry_is_refused_rather_than_failing(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "farthest")
    login(web, admin)
    for stored in (FAR, "9998-12-30 00:00:00"):
        query("UPDATE instances SET expires_at = ?", (stored,))
        response = web.post(f"/admin/instances/{wiki['id']}/extend", data={"extra_days": "30"})
        assert response.status_code == 302
        page = web.get(response.headers["Location"]).get_data(as_text=True)
        assert "cannot be extended past the year 9998" in page
        assert _expiry(query) == stored


def test_shortening_a_far_expiry_keeps_the_wiki(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "lasting")
    login(web, admin)
    query("UPDATE instances SET expires_at = ?", (FAR,))  # stored by an earlier release
    web.post(f"/admin/instances/{wiki['id']}/shorten-expiry", data={"shorten_days": "1"})
    row = query("SELECT status, expires_at FROM instances", one=True)
    assert row["status"] != "terminated" and row["expires_at"] == "9999-12-30 00:00:00"
    page = web.get(f"/admin/instances/{wiki['id']}")
    assert page.status_code == 200 and "9999-12-30 00:00 UTC" in page.get_data(as_text=True)


def test_lifting_a_suspension_keeps_a_far_expiry(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "suspended-far")
    login(web, admin)
    web.post(f"/admin/instances/{wiki['id']}/suspend", data={"suspend_duration": "permanent"})
    query("UPDATE instances SET expires_at = ?, suspended_at = '2000-01-01 00:00:00'", (FAR,))
    response = web.post(f"/admin/instances/{wiki['id']}/unsuspend")
    assert response.status_code == 302
    row = query("SELECT status, expires_at FROM instances", one=True)
    assert row["status"] == "running" and row["expires_at"] == FAR
