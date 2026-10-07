"""Creating, editing, conflicts, details, hiding, deletion and the interceptors."""

from flask import redirect

from bananawiki.wiki.features.pages import service

from .pages_support import add_interceptor, get_page, in_app, make_category, make_page, restrict


def _slug_page(app, slug):
    return in_app(app, lambda: service.get_by_slug(slug))


# ── Create ────────────────────────────────────────────────────────────────────


def test_editor_creates_page_with_category_preselected(app, client, make_user, login):
    cat = make_category(app, "Notes")
    login(client, make_user("edx1", role="editor"))
    form = client.get(f"/create?category_id={cat['id']}").get_data(as_text=True)
    assert f'value="{cat["id"]}" selected' in form
    response = client.post("/create-page", data={"title": "My note", "content": "hi", "category_id": cat["id"]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/page/my-note")
    assert _slug_page(app, "my-note")["category_id"] == cat["id"]


def test_create_requires_page_create_permission(app, client, make_user, login, db):
    plain = make_user("plainuser")
    login(client, plain)
    assert client.get("/create").status_code == 403
    assert client.post("/create", data={"title": "X"}).status_code == 403
    editor = make_user("noperm", role="editor")
    restrict(db, editor, keys={"page.edit_all"})
    client.post("/logout")
    login(client, editor)
    assert client.post("/create", data={"title": "Y"}).status_code == 403
    assert _slug_page(app, "y") is None


def test_create_respects_category_write_restrictions(app, client, make_user, login, db):
    allowed = make_category(app, "Allowed")
    other = make_category(app, "Other")
    editor = make_user("restricted", role="editor")
    restrict(db, editor, write=[allowed["id"]])
    login(client, editor)
    assert client.post("/create", data={"title": "Nope", "category_id": other["id"]}).status_code == 403
    assert client.post("/create", data={"title": "Nope2"}).status_code == 403  # uncategorised
    assert client.post("/create", data={"title": "Yes", "category_id": allowed["id"]}).status_code == 302


def test_create_denied_interceptor(app, client, make_user, login):
    add_interceptor(app, "page.create_denied", lambda user, category_id: redirect("/propose"))
    login(client, make_user("user7"))
    response = client.get("/create")
    assert response.status_code == 302 and response.headers["Location"] == "/propose"


def test_create_validation_keeps_form(admin_client):
    response = admin_client.post("/create", data={"title": "", "content": "keep me"})
    assert response.status_code == 400 and b"keep me" in response.data


def test_saved_interceptor_called_on_create_and_edit(app, admin_client):
    seen = []
    add_interceptor(app, "page.saved", lambda page, user: seen.append(page["slug"]))
    admin_client.post("/create", data={"title": "Saved one"})
    page = _slug_page(app, "saved-one")
    admin_client.post("/page/saved-one/edit", data={"title": "Saved one", "content": "x", "revision": page["revision"]})
    assert seen == ["saved-one", "saved-one"]


# ── Edit ──────────────────────────────────────────────────────────────────────


def test_edit_saves_and_records_history(app, admin_client):
    page = make_page(app, "Doc", "one")
    response = admin_client.post("/page/doc/edit", data={"title": "Doc", "content": "two", "edit_message": "fix",
                                                          "revision": page["revision"]})
    assert response.status_code == 302
    updated = get_page(app, page["id"])
    assert updated["content"] == "two" and updated["revision"] == 2
    assert in_app(app, lambda: service.history(page["id"]))[0]["edit_message"] == "fix"


def test_edit_conflict_shows_diff_and_keeps_text(app, admin_client, admin):
    page = make_page(app, "Shared", "base text")
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"], content="their text"))
    response = admin_client.post("/page/shared/edit", data={"title": "Shared", "content": "my text",
                                                             "revision": page["revision"]})
    assert response.status_code == 409
    body = response.get_data(as_text=True)
    assert "my text" in body and "<del>their</del>" in body and 'name="revision" value="2"' in body
    assert get_page(app, page["id"])["content"] == "their text"
    again = admin_client.post("/page/shared/edit", data={"title": "Shared", "content": "merged", "revision": 2})
    assert again.status_code == 302 and get_page(app, page["id"])["content"] == "merged"


def test_edit_conflict_diff_is_bounded(app, admin_client, admin):
    """R-04: a conflict between two repetitive versions shows a coarse diff instead of hanging."""
    page = make_page(app, "Lines", "a\n" * 20_000)
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"], content="b\n" + "a\n" * 20_000))
    response = admin_client.post("/page/lines/edit", data={"title": "Lines", "content": "a\n" * 20_000 + "c",
                                                            "revision": page["revision"]})
    assert response.status_code == 409
    assert "too different to compare in detail" in response.get_data(as_text=True)


def test_edit_requires_edit_all_and_category_write(app, client, make_user, login, db):
    locked = make_category(app, "Locked")
    page = make_page(app, "Guarded", "x", category_id=locked["id"])
    editor = make_user("ed2", role="editor")
    restrict(db, editor, keys={"page.create"})
    login(client, editor)
    assert client.get("/page/guarded/edit").status_code == 403
    restrict(db, editor, write=[], read=[locked["id"]])
    assert client.post("/page/guarded/edit", data={"content": "hack", "revision": 1}).status_code == 403
    assert get_page(app, page["id"])["content"] == "x"


def test_plain_users_get_edit_denied_interceptor(app, client, make_user, login):
    make_page(app, "Proposal")
    add_interceptor(app, "page.edit_denied", lambda page, user: redirect(f"/page/{page['slug']}/propose"))
    login(client, make_user("user8"))
    response = client.get("/page/proposal/edit")
    assert response.status_code == 302 and response.headers["Location"].endswith("/propose")


def test_edit_blocked_interceptor(app, admin_client):
    page = make_page(app, "Protected", "safe")
    add_interceptor(app, "page.edit_blocked", lambda page, user: "pages.blocked.pending_deletion")
    response = admin_client.post("/page/protected/edit", data={"content": "changed", "revision": 1})
    assert response.status_code == 302
    assert get_page(app, page["id"])["content"] == "safe"
    assert admin_client.post("/page/protected/delete").status_code == 302
    assert get_page(app, page["id"]) is not None


def test_edit_without_metadata_permission_ignores_title_and_category(app, client, make_user, login, db):
    cat = make_category(app, "Elsewhere")
    page = make_page(app, "Fixed title", "a")
    editor = make_user("ed3", role="editor")
    restrict(db, editor, keys={"page.edit_all", "page.create"})
    login(client, editor)
    client.post("/page/fixed-title/edit", data={"title": "New title", "content": "b", "category_id": cat["id"],
                                                 "revision": 1})
    updated = get_page(app, page["id"])
    assert updated["title"] == "Fixed title" and updated["content"] == "b" and updated["category_id"] is None


def test_pending_deletion_page_cannot_be_edited(app, admin_client):
    page = make_page(app, "Doomed", "x")
    in_app(app, lambda: service.set_fields(page["id"], pending_deletion=1))
    assert admin_client.post("/page/doomed/edit", data={"content": "y", "revision": 1}).status_code == 302
    assert get_page(app, page["id"])["content"] == "x"


# ── Details ───────────────────────────────────────────────────────────────────


def test_title_move_and_rename(app, admin_client):
    cat = make_category(app, "Target")
    page = make_page(app, "Old name", "text")
    linker = make_page(app, "Linker", "see [it](/page/old-name) and /page/old-name-archive")
    admin_client.post("/page/old-name/edit/title", data={"title": "New name"})
    admin_client.post("/page/old-name/move", data={"category_id": cat["id"]})
    response = admin_client.post("/page/old-name/rename", data={"new_slug": "fresh"})
    assert response.headers["Location"].endswith("/page/fresh")
    updated = get_page(app, page["id"])
    assert (updated["title"], updated["category_id"], updated["slug"]) == ("New name", cat["id"], "fresh")
    assert get_page(app, linker["id"])["content"] == "see [it](/page/fresh) and /page/old-name-archive"


def test_rename_to_taken_slug_refused(app, admin_client):
    make_page(app, "One")
    make_page(app, "Two")
    admin_client.post("/page/one/rename", data={"new_slug": "two"})
    assert _slug_page(app, "one") is not None


def test_details_need_edit_metadata(app, client, make_user, login, db):
    make_page(app, "Meta")
    editor = make_user("ed4", role="editor")
    restrict(db, editor, keys={"page.edit_all"})
    login(client, editor)
    assert client.post("/page/meta/edit/title", data={"title": "X"}).status_code == 403
    assert client.post("/page/meta/rename", data={"new_slug": "x"}).status_code == 403
    assert client.post("/page/meta/move", data={"category_id": ""}).status_code == 403


def test_move_to_unwritable_category_refused(app, client, make_user, login, db):
    mine = make_category(app, "Mine")
    theirs = make_category(app, "Theirs")
    page = make_page(app, "Movable", category_id=mine["id"])
    editor = make_user("ed5", role="editor")
    restrict(db, editor, write=[mine["id"]])
    login(client, editor)
    assert client.post("/page/movable/move", data={"category_id": theirs["id"]}).status_code == 403
    assert get_page(app, page["id"])["category_id"] == mine["id"]


def test_deindex_requires_permission(app, client, make_user, login, db, admin_client):
    page = make_page(app, "Visible")
    editor = make_user("ed6", role="editor")
    other = app.test_client()
    login(other, editor)
    assert other.post("/page/visible/deindex").status_code == 403
    admin_client.post("/page/visible/deindex")
    assert get_page(app, page["id"])["is_deindexed"] == 1
    admin_client.post("/page/visible/deindex")
    assert get_page(app, page["id"])["is_deindexed"] == 0


# ── Delete and home ───────────────────────────────────────────────────────────


def test_delete_requires_page_delete(app, client, make_user, login, db):
    page = make_page(app, "Victim")
    editor = make_user("ed7", role="editor")
    login(client, editor)
    assert client.post("/page/victim/delete").status_code == 403
    restrict(db, editor, keys={"page.edit_all", "page.delete"})
    assert client.post("/page/victim/delete").status_code == 302
    assert get_page(app, page["id"]) is None


def test_delete_interceptor_takes_over(app, admin_client):
    page = make_page(app, "Soft")
    add_interceptor(app, "page.delete", lambda page, user: redirect("/scheduled"))
    response = admin_client.post("/page/soft/delete")
    assert response.headers["Location"] == "/scheduled"
    assert get_page(app, page["id"]) is not None


def test_home_page_cannot_be_deleted_and_set_home_is_admin_only(app, admin_client, make_user, login):
    page = make_page(app, "Landing")
    editor = make_user("ed8", role="editor")
    client = app.test_client()
    login(client, editor)
    assert client.post("/page/landing/set-home").status_code == 403
    assert admin_client.post("/page/landing/set-home").status_code == 302
    assert get_page(app, page["id"])["is_home"] == 1
    admin_client.post("/page/landing/delete")
    assert get_page(app, page["id"]) is not None
    assert admin_client.post(f"/api/pages/{page['id']}/home").get_json()["already_home"] is True
    assert client.post(f"/api/pages/{page['id']}/home").status_code == 403


def test_forms_require_csrf(app, csrf_client, admin):
    from tests.conftest import PASSWORD, csrf_token_from

    make_page(app, "Csrf")
    token = csrf_token_from(csrf_client.get("/login"))
    csrf_client.post("/login", data={"username": admin["username"], "password": PASSWORD, "csrf_token": token})
    assert csrf_client.post("/page/csrf/delete").status_code == 400
    token = csrf_token_from(csrf_client.get("/page/csrf"))
    assert csrf_client.post("/page/csrf/delete", data={"csrf_token": token}).status_code == 302


def test_markdown_save_clears_builder_json(app, admin_client, db):
    page = make_page(app, "Built page", "md")
    db.execute("UPDATE pages SET builder_json = '{\"blocks\":[]}', builder_public = 1 WHERE id = ?", (page["id"],))
    admin_client.post("/page/built-page/edit", data={"title": "Built page", "content": "md2", "revision": 1})
    updated = get_page(app, page["id"])
    assert updated["builder_json"] == "" and updated["content"] == "md2"
