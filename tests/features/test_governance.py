"""Page governance: protection, reservations, quotas and their routes."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.page_governance import protection, quota, reservations
from bananawiki.wiki.features.page_governance.errors import GovernanceError
from bananawiki.wiki.features.pages import service as pages

from .governance_support import as_user, build_app, set_settings


@pytest.fixture
def app(app_factory):
    application = build_app(app_factory, "page_governance")
    set_settings(application, page_protection_enabled=1, page_reservations_enabled=1)
    return application


@pytest.fixture
def ctx(app):
    with app.test_request_context(), connection_scope():
        yield


@pytest.fixture
def people(make_user):
    return {
        "ed1": make_user("ed1", role="editor"),
        "ed2": make_user("ed2", role="editor"),
        "admin": make_user("boss", role="admin"),
        "reader": make_user("reader"),
    }


@pytest.fixture
def page(app):
    with app.test_request_context(), connection_scope():
        return pages.create("Governed", "text", author_id=None)


def _page(page_id):
    return pages.get(page_id)


# ── Protection ───────────────────────────────────────────────────────────────


def test_protection_blocks_everyone_but_the_controller(ctx, people, page):
    protection.protect(page, people["ed1"])
    current = _page(page["id"])
    assert registry.intercept("page.edit_blocked", page=current, user=people["ed1"]) is None
    assert registry.intercept("page.edit_blocked", page=current, user=people["ed2"]) == "page_governance.blocked.protected"
    # administrators too: they must go through the unlock delay
    assert registry.intercept("page.edit_blocked", page=current, user=people["admin"]) == "page_governance.blocked.protected"
    with pytest.raises(GovernanceError) as err:
        protection.unprotect(current, people["ed2"])
    assert err.value.key == "page_governance.protection.error.not_controller"
    with pytest.raises(GovernanceError):
        protection.protect(current, people["ed2"])
    protection.unprotect(current, people["ed1"])
    assert registry.intercept("page.edit_blocked", page=_page(page["id"]), user=people["ed2"]) is None


def test_protection_needs_edit_rights_and_not_home(ctx, people, page):
    with pytest.raises(GovernanceError):
        protection.protect(page, people["reader"])
    with pytest.raises(GovernanceError) as err:
        protection.protect(pages.home(), people["ed1"])
    assert err.value.key == "page_governance.protection.error.home"


def test_admin_unlock_waits_72_hours(ctx, db, people, page):
    protection.protect(page, people["ed1"])
    assert protection.request_unlock(page, people["admin"]) is False
    with pytest.raises(GovernanceError) as err:
        protection.force_unlock(page, people["admin"])
    assert err.value.key == "page_governance.protection.error.unlock_not_ready"
    first = _page(page["id"])["protection_unlock_requested_at"]
    protection.request_unlock(page, people["admin"])
    assert _page(page["id"])["protection_unlock_requested_at"] == first  # the clock is not reset
    db.execute("UPDATE pages SET protection_unlock_requested_at = ? WHERE id = ?", (sql_in(hours=-73), page["id"]))
    protection.force_unlock(page, people["admin"])
    assert _page(page["id"])["protected_by"] is None
    with pytest.raises(GovernanceError):
        protection.request_unlock(page, people["ed2"])


def test_force_unlock_requires_a_request(ctx, people, page):
    protection.protect(page, people["ed1"])
    with pytest.raises(GovernanceError) as err:
        protection.force_unlock(page, people["admin"])
    assert err.value.key == "page_governance.protection.error.unlock_not_requested"


def test_protection_lapses_when_controller_is_demoted(ctx, db, people, page):
    protection.protect(page, people["ed1"])
    db.execute("UPDATE users SET role = 'user' WHERE id = ?", (people["ed1"]["id"],))
    assert not protection.blocks(_page(page["id"]), people["ed2"])
    protection.force_unlock(page, people["admin"])  # lapsed protection is cleared at once
    assert _page(page["id"])["protected_by"] is None


def test_protection_off_when_setting_or_feature_off(app, ctx, people, page):
    protection.protect(page, people["ed1"])
    set_settings(app, page_protection_enabled=0)
    as_user(None)
    assert not protection.blocks(_page(page["id"]), people["ed2"])
    set_settings(app, page_protection_enabled=1)
    as_user(None)
    registry.set_enabled("page_governance", False)
    assert registry.intercept("page.edit_blocked", page=_page(page["id"]), user=people["ed2"]) is None


# ── Reservations ─────────────────────────────────────────────────────────────


def test_reserve_blocks_other_editors_but_not_admins(ctx, people, page):
    row = reservations.reserve(page, people["ed1"])
    assert row["user_id"] == people["ed1"]["id"]
    as_user(people["ed2"])
    assert registry.intercept("page.edit_blocked", page=page, user=people["ed2"]) == "page_governance.blocked.reserved"
    assert registry.intercept("page.edit_blocked", page=page, user=people["admin"]) is None
    assert registry.intercept("page.edit_blocked", page=page, user=people["ed1"]) is None
    with pytest.raises(GovernanceError) as err:
        reservations.reserve(page, people["ed2"])
    assert err.value.key == "page_governance.reservation.error.reserved_by"
    with pytest.raises(GovernanceError):
        reservations.reserve(page, people["ed1"])


def test_release_starts_cooldown_and_quota_applies(app, ctx, people):
    first = pages.create("One", author_id=None)
    second = pages.create("Two", author_id=None)
    reservations.reserve(first, people["ed1"])
    assert not reservations.release(first, people["ed2"])
    assert reservations.release(first, people["ed1"])
    with pytest.raises(GovernanceError) as err:
        reservations.reserve(first, people["ed1"])
    assert err.value.key == "page_governance.reservation.error.cooldown"
    reservations.reserve(first, people["ed2"])  # cooldowns are per user
    quota.set_quota(people["ed1"]["id"], 1)
    reservations.reserve(second, people["ed1"])
    third = pages.create("Three", author_id=None)
    with pytest.raises(GovernanceError) as err:
        reservations.reserve(third, people["ed1"])
    assert err.value.key == "page_governance.reservation.error.quota"
    quota.set_quota(people["ed1"]["id"], quota.UNLIMITED)
    reservations.reserve(third, people["ed1"])


def test_reservation_of_demoted_holder_is_ignored(ctx, db, people, page):
    reservations.reserve(page, people["ed1"])
    db.execute("UPDATE users SET role = 'user' WHERE id = ?", (people["ed1"]["id"],))
    assert reservations.current(page["id"]) is None
    reservations.reserve(page, people["ed2"])


def test_expired_reservations_and_cleanup(ctx, db, people, page):
    reservations.reserve(page, people["ed1"])
    db.execute("UPDATE page_reservations SET expires_at = ? WHERE page_id = ?", (sql_in(hours=-1), page["id"]))
    assert reservations.current(page["id"]) is None
    reservations.cleanup()
    reservations.cleanup()  # idempotent
    assert db.scalar("SELECT released_at IS NOT NULL FROM page_reservations WHERE page_id = ?", (page["id"],)) == 1


def test_admin_assign_and_force_release(ctx, people, page):
    reservations.reserve(page, people["ed1"])
    with pytest.raises(GovernanceError):
        reservations.assign(page, people["reader"])  # cannot edit
    reservations.assign(page, people["ed2"])
    assert reservations.current(page["id"])["user_id"] == people["ed2"]["id"]
    assert reservations.force_release(page["id"])
    assert reservations.cooldown_until(page["id"], people["ed2"]["id"]) is None


def test_home_and_pending_pages_cannot_be_reserved(ctx, db, people, page):
    with pytest.raises(GovernanceError):
        reservations.reserve(pages.home(), people["ed1"])
    db.execute("UPDATE pages SET pending_deletion = 1 WHERE id = ?", (page["id"],))
    with pytest.raises(GovernanceError):
        reservations.reserve(_page(page["id"]), people["ed1"])


def test_delete_interceptor_refuses_blocked_pages(app, people, page):
    with app.test_request_context("/page/governed/delete", method="POST"), connection_scope():
        as_user(people["ed2"])
        protection.protect(page, people["ed1"])
        response = registry.intercept("page.delete", page=_page(page["id"]), user=people["ed2"])
        assert response is not None and response.status_code == 302
        assert registry.intercept("page.delete", page=_page(page["id"]), user=people["ed1"]) is None


def test_reserve_after_save(app, people, page):
    with app.test_request_context("/page/governed/edit", method="POST", data={"reserve_after_save": "1"}), \
            connection_scope():
        as_user(people["ed1"])
        registry.intercept("page.saved", page=page, user=people["ed1"])
        assert reservations.current(page["id"])["user_id"] == people["ed1"]["id"]


# ── Quota requests ───────────────────────────────────────────────────────────


def test_quota_requests_auto_approve_cooldown_and_review(app, ctx, db, people):
    set_settings(app, reservation_quota_auto_approve_max=8, quota_request_cooldown_hours=2)
    as_user(None)
    user_id = people["ed1"]["id"]
    with pytest.raises(GovernanceError):
        quota.create_request(user_id, 7, "")
    row = quota.create_request(user_id, 7, "busy month")
    assert row["status"] == "approved" and row["review_source"] == "automatic"
    assert quota.effective(user_id) == 7
    with pytest.raises(GovernanceError) as err:
        quota.create_request(user_id, 20, "more")
    assert err.value.key == "page_governance.quota.error.cooldown"
    db.execute("UPDATE users SET quota_request_cooldown_until = NULL WHERE id = ?", (user_id,))
    pending = quota.create_request(user_id, 20, "more")
    assert pending["status"] == "pending"
    with pytest.raises(GovernanceError):
        quota.create_request(user_id, 30, "again")
    with pytest.raises(GovernanceError):
        quota.cancel(pending["id"], people["ed2"]["id"])  # not theirs
    quota.review(pending["id"], people["admin"]["id"], approve=True)
    assert quota.effective(user_id) == 20
    with pytest.raises(GovernanceError):
        quota.review(pending["id"], people["admin"]["id"], approve=False)


# ── Routes ───────────────────────────────────────────────────────────────────


def test_reserve_and_release_routes(client, login, people, page):
    login(client, people["ed1"])
    response = client.post(f"/page/{page['slug']}/reserve")
    assert response.status_code == 302
    status = client.get(f"/api/pages/{page['id']}/reservation/status").get_json()
    assert status["is_reserved"] and status["reserved_by_username"] == "ed1"
    assert client.delete(f"/api/pages/{page['id']}/reservation").get_json()["ok"]
    assert client.get("/reservations").status_code == 200


def test_routes_refuse_readers_and_hidden_pages(client, login, people, page, db):
    login(client, people["reader"])
    assert client.post(f"/page/{page['slug']}/reserve").status_code == 403
    assert client.get("/admin/checkouts").status_code == 403
    client.post("/logout")
    login(client, people["ed1"])
    db.execute("UPDATE pages SET is_deindexed = 1 WHERE id = ?", (page["id"],))
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (people["ed1"]["id"],))
    assert client.post(f"/page/{page['slug']}/protection", data={"action": "protect"}).status_code == 404


def test_protection_route(client, login, people, page):
    login(client, people["ed1"])
    client.post(f"/page/{page['slug']}/protection", data={"action": "protect"})
    with client.application.test_request_context(), connection_scope():
        assert pages.get(page["id"])["protected_by"] == people["ed1"]["id"]
    assert client.post(f"/page/{page['slug']}/protection", data={"action": "bogus"}).status_code == 400


def test_admin_pages_render_and_act(client, login, people, page):
    login(client, people["admin"])
    for url in ("/admin/checkouts", "/admin/governance", f"/admin/users/{people['ed1']['id']}/reservation-quota"):
        assert client.get(url).status_code == 200, url
    client.post("/admin/checkouts/assign-new", data={"page_slug": page["slug"], "user_id": people["ed1"]["id"]})
    with client.application.test_request_context(), connection_scope():
        assert reservations.current(page["id"])["user_id"] == people["ed1"]["id"]
    client.post(f"/admin/checkouts/{page['id']}/release")
    response = client.post("/admin/governance", data={
        "page_protection_enabled": "1", "page_reservations_enabled": "1", "page_reservation_duration_hours": "5",
        "page_reservation_cooldown_hours": "0", "default_reserved_pages_quota": "3",
        "reservation_quota_auto_approve_max": "0", "quota_request_cooldown_hours": "0"})
    assert response.status_code == 302
    with client.application.test_request_context(), connection_scope():
        assert reservations.duration_hours() == 5
    # 1.4 unlock URL still answers
    assert client.post(f"/admin/settings/page-protection/{page['id']}/request-unlock").status_code == 302


def test_quota_page_for_editor(client, login, people):
    login(client, people["ed1"])
    assert client.get("/account/reservation-quota").status_code == 301
    assert client.get("/settings/reservation-quota").status_code == 200
    client.post("/settings/reservation-quota", data={"action": "submit_quota_request", "requested_quota": "9",
                                                     "reason": "please"})
    with client.application.test_request_context(), connection_scope():
        assert quota.pending_request(people["ed1"]["id"])["requested_quota"] == 9
    # editors cannot set their own quota
    assert client.post("/settings/reservation-quota", data={"action": "set_quota", "new_quota": "99"}).status_code == 400


def test_feature_off_answers_404(app, client, login, people):
    with app.test_request_context(), connection_scope():
        registry.set_enabled("page_governance", False)
    login(client, people["admin"])
    assert client.get("/admin/checkouts").status_code == 404


def test_slots_render(app, people, page):
    with app.test_request_context(f"/page/{page['slug']}"), connection_scope():
        reservations.reserve(page, people["ed1"])
        protection.protect(page, people["ed1"])
        as_user(people["ed2"])
        notices = registry.render_slot("page.above_content", page=pages.get(page["id"]))
        assert "ed1" in notices
        as_user(people["ed1"])
        actions = registry.render_slot("page.header_actions", page=pages.get(page["id"]))
        assert "reservation/release" in actions and 'value="unprotect"' in actions
        other = pages.create("Free", author_id=None)
        assert "reserve_after_save" in registry.render_slot("editor.below_form", page=other)
