"""Difficulty tags: header form, editor fields, validation, permissions and display."""

from bananawiki.wiki.features.pages import service

from .pages_support import get_page, in_app, make_category, make_page, restrict, set_feature


def _tag(client, slug, tag, label="", color=""):
    return client.post(f"/page/{slug}/tag", data={"difficulty_tag": tag, "tag_custom_label": label,
                                                   "tag_custom_color": color})


def _fields(app, page):
    row = get_page(app, page["id"])
    return row["difficulty_tag"], row["tag_custom_label"], row["tag_custom_color"]


def test_editor_sets_level_and_it_is_shown(app, client, make_user, login):
    page = make_page(app, "Lesson")
    login(client, make_user("tag_ed", role="editor"))
    assert _tag(client, "lesson", "expert").status_code == 302
    assert _fields(app, page) == ("expert", "", "")
    html = client.get("/page/lesson").get_data(as_text=True)
    assert "difficulty-tag--expert" in html and "Expert" in html


def test_custom_tag_is_validated_and_normalised(app, client, admin_client):
    page = make_page(app, "Custom")
    _tag(admin_client, "custom", "custom", "", "#abc")
    assert _fields(app, page) == ("", "", "")
    _tag(admin_client, "custom", "custom", "Exam", "red")
    assert _fields(app, page) == ("", "", "")
    _tag(admin_client, "custom", "custom", "x" * 51, "#abc")
    assert _fields(app, page) == ("", "", "")
    _tag(admin_client, "custom", "custom", "  Exam\x00 topic ", "#ABC")
    assert _fields(app, page) == ("custom", "Exam topic", "#aabbcc")
    html = admin_client.get("/page/custom").get_data(as_text=True)
    assert "--tag-color: #aabbcc" in html and "Exam topic" in html
    _tag(admin_client, "custom", "beginner", "ignored", "#123456")
    assert _fields(app, page) == ("beginner", "", "")


def test_label_is_escaped(app, admin_client):
    make_page(app, "Escape")
    _tag(admin_client, "escape", "custom", "<script>x</script>", "#112233")
    html = admin_client.get("/page/escape").get_data(as_text=True)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_invalid_tag_is_refused(app, admin_client):
    page = make_page(app, "Invalid")
    _tag(admin_client, "invalid", "legendary")
    assert _fields(app, page) == ("", "", "")


def test_permissions_split_between_levels_and_custom(app, client, make_user, login, db):
    page = make_page(app, "Split")
    editor = make_user("tag_custom_only", role="editor")
    restrict(db, editor, keys={"page.edit_all", "tag.edit_custom"})
    login(client, editor)
    assert _tag(client, "split", "easy").status_code == 403
    assert _tag(client, "split", "custom", "Mine", "#123456").status_code == 302
    assert _fields(app, page)[0] == "custom"
    assert _tag(client, "split", "").status_code == 302
    assert _fields(app, page) == ("", "", "")


def test_users_and_restricted_editors_cannot_tag(app, client, make_user, login, db):
    cat = make_category(app, "Other")
    page = make_page(app, "Guarded", category_id=cat["id"])
    login(client, make_user("tag_user"))
    assert _tag(client, "guarded", "easy").status_code == 403
    client.post("/logout")
    editor = make_user("tag_ro", role="editor")
    restrict(db, editor, read=[cat["id"]], write=[])
    login(client, editor)
    assert _tag(client, "guarded", "easy").status_code == 403
    assert 'data-dialog-open="dlg-difficulty-tag"' not in client.get("/page/guarded").get_data(as_text=True)
    assert _fields(app, page) == ("", "", "")


def test_editor_form_sets_tag_on_save(app, client, admin_client):
    page = make_page(app, "Edited", "v1")
    editor = admin_client.get("/page/edited/edit").get_data(as_text=True)
    assert 'name="difficulty_tag"' in editor
    admin_client.post("/page/edited/edit", data={"title": "Edited", "content": "v2", "revision": 1,
                                                 "difficulty_tag": "intermediate"})
    assert _fields(app, page) == ("intermediate", "", "")


def test_new_page_form_sets_tag(app, client, make_user, login, db):
    editor = make_user("tag_creator", role="editor")
    restrict(db, editor, keys={"page.create", "tag.edit_difficulty"})
    login(client, editor)
    assert 'name="difficulty_tag"' in client.get("/create").get_data(as_text=True)
    client.post("/create", data={"title": "Fresh", "content": "x", "difficulty_tag": "easy"})
    created = in_app(app, lambda: service.get_by_slug("fresh"))
    assert created["difficulty_tag"] == "easy"


def test_editor_without_tag_fields_keeps_tag(app, client, admin_client, db):
    page = make_page(app, "Keep")
    db.execute("UPDATE pages SET difficulty_tag = 'expert' WHERE id = ?", (page["id"],))
    admin_client.post("/page/keep/edit", data={"title": "Keep", "content": "v2", "revision": 1})
    assert _fields(app, page)[0] == "expert"


def test_disabled_feature_hides_tag(app, admin_client, db):
    page = make_page(app, "Hidden tag")
    db.execute("UPDATE pages SET difficulty_tag = 'expert' WHERE id = ?", (page["id"],))
    set_feature(app, "difficulty_tags", False)
    assert "difficulty-tag--expert" not in admin_client.get("/page/hidden-tag").get_data(as_text=True)
    assert _tag(admin_client, "hidden-tag", "easy").status_code == 404
    admin_client.post("/page/hidden-tag/edit", data={"title": "Hidden tag", "content": "x", "revision": 1,
                                                     "difficulty_tag": "easy"})
    assert _fields(app, page)[0] == "expert"
