"""Page history: listing, versions and diffs, revert, deletion, attribution, feature switch."""

from bananawiki.wiki.features.pages import service

from .pages_support import add_interceptor, get_page, in_app, make_category, make_page, restrict, set_feature


def _history(app, page_id):
    return in_app(app, lambda: service.history(page_id))


def _two_versions(app, admin):
    page = make_page(app, "Story", "once upon a time", author_id=admin["id"])
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"],
                                       content="once upon a rainy time", edit_message="weather"))
    return page


def _client(app, make_user, login, name, role="user"):
    client = app.test_client()
    user = make_user(name, role=role)
    login(client, user)
    return client, user


def test_history_list_and_entry_views(app, admin_client, admin):
    page = _two_versions(app, admin)
    body = admin_client.get("/page/story/history").get_data(as_text=True)
    assert "weather" in body and "+6" in body
    newest = _history(app, page["id"])[0]["id"]
    diff = admin_client.get(f"/page/story/history/{newest}?view=source_diff").get_data(as_text=True)
    assert "<ins>rainy</ins>" in diff
    rendered = admin_client.get(f"/page/story/history/{newest}?view=diff").get_data(as_text=True)
    assert "<ins>rainy</ins>" in rendered
    oldest = _history(app, page["id"])[-1]["id"]
    assert admin_client.get(f"/page/story/history/{oldest}?view=diff").status_code == 200
    assert admin_client.get(f"/page/story/history/{newest}?against={oldest}&view=source").status_code == 200


def test_entry_of_other_page_is_404(app, admin_client, admin):
    _two_versions(app, admin)
    other = make_page(app, "Other")
    entry = _history(app, other["id"])[0]["id"]
    assert admin_client.get(f"/page/story/history/{entry}").status_code == 404
    assert admin_client.post(f"/page/story/revert/{entry}").status_code == 404


def test_history_needs_view_permission_and_visibility(app, make_user, login, db, admin):
    secret = make_category(app, "Classified")
    make_page(app, "Plans", category_id=secret["id"])
    _two_versions(app, admin)
    client, user = _client(app, make_user, login, "historian")
    assert client.get("/page/story/history").status_code == 200
    restrict(db, user, keys={"page.view_all"})
    assert client.get("/page/story/history").status_code == 403
    restrict(db, user, read=[])
    assert client.get("/page/plans/history").status_code == 404


def test_history_disabled_feature_is_404(app, admin_client, admin):
    _two_versions(app, admin)
    set_feature(app, "page_history", False)
    assert admin_client.get("/page/story/history").status_code == 404
    assert b"/page/story/history" not in admin_client.get("/page/story").data
    set_feature(app, "page_history", True)
    assert admin_client.get("/page/story/history").status_code == 200


def test_revert_needs_permission(app, make_user, login, db, admin):
    page = _two_versions(app, admin)
    first = _history(app, page["id"])[-1]["id"]
    client, editor = _client(app, make_user, login, "reverter", "editor")
    assert client.post(f"/page/story/revert/{first}").status_code == 403
    restrict(db, editor, keys={"page.edit_all", "history.revert"})
    assert client.post(f"/page/story/revert/{first}").status_code == 302
    reverted = get_page(app, page["id"])
    assert reverted["content"] == "once upon a time"
    assert _history(app, page["id"])[0]["is_revert"] == 1


def test_revert_blocked_by_interceptor(app, admin_client, admin):
    page = _two_versions(app, admin)
    first = _history(app, page["id"])[-1]["id"]
    add_interceptor(app, "page.edit_blocked", lambda page, user: "pages.blocked.pending_deletion")
    admin_client.post(f"/page/story/revert/{first}")
    assert get_page(app, page["id"])["content"] == "once upon a rainy time"


def test_delete_entry_and_clear(app, make_user, login, db, admin, admin_client):
    page = _two_versions(app, admin)
    entry = _history(app, page["id"])[0]["id"]
    client, editor = _client(app, make_user, login, "cleaner", "editor")
    assert client.post(f"/page/story/history/{entry}/delete").status_code == 403
    assert client.post("/page/story/history/clear").status_code == 403
    admin_client.post(f"/page/story/history/{entry}/delete")
    assert len(_history(app, page["id"])) == 1
    admin_client.post("/page/story/history/clear")
    assert _history(app, page["id"]) == []
    assert get_page(app, page["id"])["content"] == "once upon a rainy time"


def test_transfer_and_deattribute(app, make_user, login, admin, admin_client):
    page = _two_versions(app, admin)
    newest, oldest = (h["id"] for h in _history(app, page["id"]))
    target = make_user("credited")
    admin_client.post(f"/page/story/history/{newest}/transfer", data={"username": "credited"})
    assert _history(app, page["id"])[0]["edited_by"] == target["id"]
    admin_client.post("/page/story/history/bulk-transfer", data={"from_user_id": admin["id"], "new_user_id": target["id"]})
    assert {h["edited_by"] for h in _history(app, page["id"])} == {target["id"]}
    admin_client.post(f"/page/story/history/{oldest}/deattribute")
    assert _history(app, page["id"])[-1]["edited_by"] is None
    admin_client.post(f"/page/story/history/{oldest}/transfer", data={"username": "ghost"})
    assert _history(app, page["id"])[-1]["edited_by"] is None
    client, _ = _client(app, make_user, login, "thief", "editor")
    assert client.post(f"/page/story/history/{newest}/transfer", data={"username": "thief"}).status_code == 403
