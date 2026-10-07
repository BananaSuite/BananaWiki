"""A wiki whose administrator set an expiry in 9999 keeps that expiry through later changes."""

from __future__ import annotations


def test_shortening_a_far_expiry_keeps_the_wiki(web, make_account, make_wiki, login, query):
    admin, user = make_account(admin=True), make_account()
    wiki = make_wiki(user, "lasting")
    login(web, admin)
    web.post(f"/admin/instances/{wiki['id']}/set-expiry", data={"expiry_datetime": "9999-12-31T00:00"})
    assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == "9999-12-31 00:00:00"
    web.post(f"/admin/instances/{wiki['id']}/shorten-expiry", data={"shorten_days": "1"})
    row = query("SELECT status, expires_at FROM instances", one=True)
    assert row["status"] != "terminated" and row["expires_at"] == "9999-12-30 00:00:00"
    page = web.get(f"/admin/instances/{wiki['id']}")
    assert page.status_code == 200 and "9999-12-30 00:00 UTC" in page.get_data(as_text=True)
