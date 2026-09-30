"""Contributions: proposing, quotas, review with diff, approval credited to the proposer."""

from __future__ import annotations

import pytest

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.contributions import quota, service
from bananawiki.wiki.features.contributions.errors import ContributionError
from bananawiki.wiki.features.page_governance import protection
from bananawiki.wiki.features.pages import service as pages

from .governance_support import as_user, build_app, set_settings


@pytest.fixture
def app(app_factory):
    return build_app(app_factory, "contributions")


@pytest.fixture
def ctx(app):
    with app.test_request_context(), connection_scope():
        yield


@pytest.fixture
def people(make_user):
    return {"reader": make_user("reader"), "other": make_user("other"), "editor": make_user("ed_one", role="editor"),
            "admin": make_user("boss", role="admin")}


@pytest.fixture
def page(app):
    with app.test_request_context(), connection_scope():
        return pages.create("Article", "line one\nline two\n", author_id=None)


def test_propose_and_approve_credits_proposer(ctx, people, page):
    as_user(people["reader"])
    assert service.can_propose(page, people["reader"])
    assert not service.can_propose(page, people["editor"])  # editors edit directly
    contribution_id = service.propose(page, people["reader"], title="Article", content="line one\nline 2\n",
                                      reason="typo")
    with pytest.raises(ContributionError) as err:
        service.propose(page, people["reader"], title="", content="x", reason="again")
    assert err.value.key == "contributions.error.already_pending"
    with pytest.raises(ContributionError):
        service.approve(contribution_id, people["reader"])  # not a reviewer
    with pytest.raises(ContributionError):
        service.approve(contribution_id, people["editor"])  # editors lack contribution.review by default
    updated = service.approve(contribution_id, people["admin"], "thanks")
    assert updated["content"] == "line one\nline 2\n"
    assert pages.history(page["id"])[0]["edited_by"] == people["reader"]["id"]
    assert service.get(contribution_id)["status"] == "approved"
    with pytest.raises(ContributionError):
        service.approve(contribution_id, people["admin"])  # only once
    # a second proposal on the same page reuses the resolved row (1.4 refused it)
    again = service.propose(pages.get(page["id"]), people["reader"], title=None, content="three", reason="more")
    assert again == contribution_id and service.get(again)["status"] == "pending"


def test_quota_and_validation(ctx, people):
    first = pages.create("A", author_id=None)
    second = pages.create("B", author_id=None)
    quota.set_quota(people["reader"]["id"], 1)
    with pytest.raises(ContributionError) as err:
        service.propose(first, people["reader"], title="A", content="x", reason="")
    assert err.value.key == "contributions.error.reason_required"
    service.propose(first, people["reader"], title="A", content="x", reason="r")
    with pytest.raises(ContributionError) as err:
        service.propose(second, people["reader"], title="B", content="x", reason="r")
    assert err.value.key == "contributions.error.quota"


def test_withdraw_edit_and_deny(ctx, people, page):
    cid = service.propose(page, people["reader"], title="T", content="c", reason="r")
    contribution = service.get(cid)
    with pytest.raises(ContributionError):
        service.update_own(contribution, people["other"], title="x", content="y", reason="z")
    service.update_own(contribution, people["reader"], title="T2", content="c2", reason="r2")
    assert service.get(cid)["title"] == "T2"
    with pytest.raises(ContributionError):
        service.withdraw(contribution, people["other"])
    service.deny(cid, people["admin"], "no")
    assert service.get(cid)["status"] == "denied"
    with pytest.raises(ContributionError):
        service.withdraw(contribution, people["reader"])


def test_approval_respects_page_protection(app, ctx, people, page):
    registry.set_enabled("page_governance", True)
    set_settings(app, page_protection_enabled=1)
    as_user(None)
    cid = service.propose(page, people["reader"], title="T", content="c", reason="r")
    protection.protect(pages.get(page["id"]), people["editor"])
    with pytest.raises(ContributionError) as err:
        service.approve(cid, people["admin"])
    assert err.value.key == "page_governance.blocked.protected"
    assert service.get(cid)["status"] == "pending"  # nothing half-applied


def test_expiry_job(app, ctx, db, people, page):
    cid = service.propose(page, people["reader"], title="T", content="c", reason="r")
    db.execute("UPDATE pending_contributions SET created_at = ? WHERE id = ?", (sql_in(hours=-50), cid))
    service.expire()
    assert service.get(cid)["status"] == "pending"  # expiry off by default
    set_settings(app, draft_expiration_hours=24)
    as_user(None)
    service.expire()
    service.expire()
    assert service.get(cid)["status"] == "expired"


def test_diff_lines():
    rows = service.diff_lines("a\nb\nc", "a\nB\nc")
    assert {"op": "del", "text": "b"} in rows and {"op": "add", "text": "B"} in rows


def test_edit_denied_interceptor(app, people, page):
    with app.test_request_context(f"/page/{page['slug']}/edit"), connection_scope():
        as_user(people["reader"])
        response = registry.intercept("page.edit_denied", page=page, user=people["reader"])
        assert response.status_code == 302 and "propose-edit" in response.location


def test_routes_flow_and_idor(client, login, people, page, make_user):
    login(client, people["reader"])
    assert client.get(f"/page/{page['slug']}/propose-edit").status_code == 200
    response = client.post(f"/page/{page['slug']}/propose-edit",
                           data={"title": "Article", "content": "new", "reason": "better"})
    assert response.status_code == 302
    assert client.get("/my-contributions").status_code == 200
    with client.application.test_request_context(), connection_scope():
        cid = service.own_pending(page["id"], people["reader"]["id"])["id"]
        other_page = pages.create("Other", author_id=None)
    assert client.get("/admin/contributions").status_code == 403
    # the contribution id must match the page in the URL
    assert client.get(f"/page/{other_page['slug']}/contribution/{cid}/edit").status_code == 404
    client.post("/logout")
    login(client, people["other"])
    assert client.post(f"/page/{page['slug']}/contribution/{cid}/withdraw").status_code == 403
    client.post("/logout")
    login(client, people["admin"])
    assert client.get("/admin/contributions").status_code == 200
    assert client.get(f"/admin/contributions/{cid}").status_code == 200
    client.post(f"/admin/contributions/{cid}/approve", data={"review_reason": "ok"})
    with client.application.test_request_context(), connection_scope():
        assert pages.get(page["id"])["content"] == "new"


def test_quota_request_routes(client, login, people):
    login(client, people["reader"])
    client.post("/my-contributions/quota-request", data={"requested_quota": "9", "reason": "busy"})
    with client.application.test_request_context(), connection_scope():
        pending = quota.pending_request(people["reader"]["id"])
    assert pending["requested_quota"] == 9
    client.post("/logout")
    login(client, people["admin"])
    client.post(f"/admin/contribution-quota-requests/{pending['id']}/review", data={"action": "approve"})
    with client.application.test_request_context(), connection_scope():
        assert quota.effective(people["reader"]["id"]) == 9


def test_feature_off(app, client, login, people, page):
    set_settings(app, contribution_approval_enabled=0)
    login(client, people["reader"])
    assert client.get(f"/page/{page['slug']}/propose-edit").status_code == 404


def test_slots_and_reviewer_scope(app, people, page, db):
    with app.test_request_context(f"/page/{page['slug']}"), connection_scope():
        as_user(people["reader"])
        assert "propose-edit" in registry.render_slot("page.header_actions", page=page)
        service.propose(page, people["reader"], title="T", content="c", reason="r")
        assert "withdraw" in registry.render_slot("page.above_content", page=page)
    db.execute("INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
               (people["editor"]["id"],))
    for key in ("page.view_all", "page.edit_all", "contribution.review"):
        db.execute("INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)", (people["editor"]["id"], key))
    with app.test_request_context(), connection_scope():
        as_user(people["editor"])
        assert service.review_count(people["editor"]) == 1
        assert "review" in registry.render_slot("page.above_content", page=page).lower()


def test_promotion_withdraws_pending_proposals(app, ctx, db, people, page):
    cid = service.propose(page, people["reader"], title="T", content="c", reason="r")
    db.execute("UPDATE users SET role = 'editor' WHERE id = ?", (people["reader"]["id"],))
    promoted = db.one("SELECT * FROM users WHERE id = ?", (people["reader"]["id"],))
    registry.emit("user.role_changed", user=promoted, old_role="user", new_role="editor",
                  changed_by=people["admin"]["id"])
    assert service.get(cid)["status"] == "withdrawn"


def test_role_change_to_user_keeps_proposals(app, ctx, db, people, page):
    cid = service.propose(page, people["reader"], title="T", content="c", reason="r")
    registry.emit("user.role_changed", user=people["reader"], old_role="user", new_role="user", changed_by=None)
    assert service.get(cid)["status"] == "pending"
