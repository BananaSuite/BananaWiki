"""Drafts: autosave API, restore/discard, other editors, credit on save, transfer, expiry."""

from bananawiki.wiki.features.drafts import service

from .pages_support import in_app, make_category, make_page, restrict, set_feature

JSON = {"Accept": "application/json"}


def _save(client, page, content="draft text", title=None, **extra):
    body = {"page_id": page["id"], "title": title or page["title"], "content": content, **extra}
    return client.post("/api/draft/save", json=body, headers=JSON)


def _draft(db, page, user):
    return db.one("SELECT * FROM drafts WHERE page_id = ? AND user_id = ?", (page["id"], user["id"]))


def test_autosave_stores_and_loads_own_draft(app, client, make_user, login, db):
    page = make_page(app, "Draft me", "original")
    editor = make_user("dr_ed", role="editor")
    login(client, editor)
    response = _save(client, page, "work in progress")
    assert response.status_code == 200 and response.get_json()["status"] == "saved"
    assert _draft(db, page, editor)["content"] == "work in progress"
    loaded = client.get(f"/api/draft/load/{page['id']}", headers=JSON).get_json()
    assert loaded["content"] == "work in progress" and loaded["title"] == "Draft me"
    _save(client, page, "second version")
    assert db.scalar("SELECT COUNT(*) FROM drafts") == 1
    assert _draft(db, page, editor)["content"] == "second version"


def test_draft_identical_to_page_is_not_kept(app, client, make_user, login, db):
    page = make_page(app, "Same", "text")
    editor = make_user("dr_same", role="editor")
    login(client, editor)
    _save(client, page, "changed")
    response = _save(client, page, "text")
    assert response.get_json()["status"] == "unchanged"
    assert _draft(db, page, editor) is None


def test_autosave_requires_edit_rights_and_permission(app, client, make_user, login, db):
    cat = make_category(app, "Locked")
    page = make_page(app, "Locked page", category_id=cat["id"])
    plain = make_user("dr_plain")
    login(client, plain)
    assert _save(client, page).status_code == 403
    client.post("/logout")
    editor = make_user("dr_ro", role="editor")
    restrict(db, editor, read=[cat["id"]], write=[])
    login(client, editor)
    assert _save(client, page).status_code == 403
    client.post("/logout")
    no_perm = make_user("dr_noperm", role="editor")
    restrict(db, no_perm, keys={"page.edit_all"})
    login(client, no_perm)
    assert _save(client, page).status_code == 403
    assert db.scalar("SELECT COUNT(*) FROM drafts") == 0


def test_invisible_page_answers_404(app, client, make_user, login, db):
    secret = make_category(app, "Secret")
    page = make_page(app, "Secret page", category_id=secret["id"])
    editor = make_user("dr_hidden", role="editor")
    restrict(db, editor, read=[], write=[])
    login(client, editor)
    assert _save(client, page).status_code == 404
    assert client.get(f"/api/draft/others/{page['id']}", headers=JSON).status_code == 404


def test_validation_and_size_limits(app, client, make_user, login, db):
    page = make_page(app, "Limits")
    login(client, make_user("dr_limits", role="editor"))
    assert client.post("/api/draft/save", data="nope", content_type="application/json",
                       headers=JSON).status_code == 400
    assert _save(client, page, content=123).status_code == 400
    assert _save(client, page, title="x" * 201).status_code == 400
    assert _save(client, page, content="x" * 1_000_001).status_code == 400
    too_big = client.post("/api/draft/save", data="x" * (8 * 1024 * 1024 + 1), content_type="application/json",
                          headers=JSON)
    assert too_big.status_code == 413
    assert db.scalar("SELECT COUNT(*) FROM drafts") == 0


def test_saving_the_page_clears_draft_and_credits_contributors(app, client, make_user, login, db):
    page = make_page(app, "Team page", "v1")
    alice = make_user("dr_alice", role="editor")
    bob = make_user("dr_bob", role="editor")
    login(client, alice)
    _save(client, page, "alice's ideas")
    client.post("/logout")
    login(client, bob)
    _save(client, page, "bob's ideas")
    editor_html = client.get("/page/team-page/edit").get_data(as_text=True)
    assert "dr_alice" in editor_html and "Restore draft" in editor_html
    response = client.post("/page/team-page/edit", data={"title": "Team page", "content": "merged",
                                                          "revision": 1, "edit_message": "merge"})
    assert response.status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM drafts WHERE page_id = ?", (page["id"],)) == 0
    message = db.scalar("SELECT edit_message FROM page_history WHERE page_id = ? ORDER BY id DESC LIMIT 1",
                        (page["id"],))
    assert message == "merge (contributors: dr_alice)"


def test_save_without_changes_only_clears_own_draft(app, client, make_user, login, db):
    page = make_page(app, "Quiet", "same")
    alice = make_user("dr_q_alice", role="editor")
    bob = make_user("dr_q_bob", role="editor")
    login(client, alice)
    _save(client, page, "alice draft")
    client.post("/logout")
    login(client, bob)
    _save(client, page, "bob draft")
    client.post("/page/quiet/edit", data={"title": "Quiet", "content": "same", "revision": 1})
    assert _draft(db, page, bob) is None
    assert _draft(db, page, alice) is not None
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page["id"],)) == 1


def test_late_autosave_after_saving_is_ignored(app, client, make_user, login, db):
    page = make_page(app, "Race", "v1")
    editor = make_user("dr_race", role="editor")
    login(client, editor)
    client.post("/page/race/edit", data={"title": "Race", "content": "v2", "revision": 1})
    response = _save(client, page, "v2 plus late keystrokes", revision=1)
    assert response.get_json()["status"] == "stale"
    assert _draft(db, page, editor) is None
    assert _save(client, page, "fresh work", revision=2).get_json()["status"] == "saved"


def test_others_endpoint_lists_names_not_content(app, client, make_user, login, db):
    page = make_page(app, "Others")
    alice = make_user("dr_o_alice", role="editor")
    login(client, alice)
    _save(client, page, "top secret draft")
    client.post("/logout")
    login(client, make_user("dr_o_bob", role="editor"))
    data = client.get(f"/api/draft/others/{page['id']}", headers=JSON).get_json()
    assert [d["username"] for d in data["drafts"]] == ["dr_o_alice"]
    assert "top secret" not in str(data)
    assert client.get(f"/api/draft/load/{page['id']}", headers=JSON).get_json()["content"] is None


def test_transfer_requires_permission(app, client, make_user, login, db, admin):
    page = make_page(app, "Handover", "base")
    alice = make_user("dr_t_alice", role="editor")
    login(client, alice)
    _save(client, page, "alice text")
    client.post("/logout")
    bob = make_user("dr_t_bob", role="editor")
    login(client, bob)
    body = {"page_id": page["id"], "from_user_id": alice["id"]}
    assert client.post("/api/draft/transfer", json=body, headers=JSON).status_code == 403
    assert client.post(f"/drafts/{page['id']}/transfer", data={"from_user_id": alice["id"]}).status_code == 403
    client.post("/logout")
    login(client, admin)
    _save(client, page, "admin's own draft")
    assert client.post("/api/draft/transfer", json=body, headers=JSON).status_code == 200
    assert _draft(db, page, alice) is None
    assert _draft(db, page, admin)["content"] == "alice text"
    assert client.post("/api/draft/transfer", json=body, headers=JSON).status_code == 404
    self_body = {"page_id": page["id"], "from_user_id": admin["id"]}
    assert client.post("/api/draft/transfer", json=self_body, headers=JSON).status_code == 400


def test_transfer_form_fallback(app, client, make_user, login, db, admin):
    page = make_page(app, "Form handover", "base")
    alice = make_user("dr_f_alice", role="editor")
    login(client, alice)
    _save(client, page, "alice text")
    client.post("/logout")
    login(client, admin)
    response = client.post(f"/drafts/{page['id']}/transfer", data={"from_user_id": alice["id"]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/page/form-handover/edit")
    assert _draft(db, page, admin)["content"] == "alice text"


def test_discard_and_my_drafts_page(app, client, make_user, login, db):
    first = make_page(app, "First draft page")
    second = make_page(app, "Second draft page")
    editor = make_user("dr_mine", role="editor")
    login(client, editor)
    _save(client, first, "one")
    _save(client, second, "two")
    html = client.get("/drafts").get_data(as_text=True)
    assert "First draft page" in html and "Second draft page" in html
    mine = client.get("/api/draft/mine", headers=JSON).get_json()
    assert {d["page_slug"] for d in mine} == {"first-draft-page", "second-draft-page"}
    response = client.post(f"/drafts/{first['id']}/discard", data={"next": "/drafts"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/drafts")
    assert client.post("/api/draft/delete", json={"page_id": second["id"]}, headers=JSON).status_code == 200
    assert db.scalar("SELECT COUNT(*) FROM drafts") == 0
    assert client.post("/drafts/1/discard", data={"next": "https://evil.example/"}).headers["Location"] \
        .startswith("/")


def test_my_drafts_hides_titles_of_pages_no_longer_visible(app, client, make_user, login, db):
    cat = make_category(app, "Was open")
    page = make_page(app, "Renamed secret title", category_id=cat["id"])
    editor = make_user("dr_lost", role="editor")
    login(client, editor)
    _save(client, page, "text", title="My own title")
    restrict(db, editor, read=[], write=[])
    html = client.get("/drafts").get_data(as_text=True)
    assert "My own title" in html and "Renamed secret title" not in html
    assert client.get("/api/draft/mine", headers=JSON).get_json() == []


def test_plain_users_have_no_drafts_page(app, client, make_user, login):
    login(client, make_user("dr_user"))
    assert client.get("/drafts").status_code == 403
    assert client.get("/api/draft/mine", headers=JSON).status_code == 403


def test_expiry_job(app, db, make_user):
    page = make_page(app, "Expiring")
    user = make_user("dr_exp", role="editor")
    db.execute("INSERT INTO drafts (page_id, user_id, title, content, updated_at) VALUES (?, ?, 't', 'c', ?)",
               (page["id"], user["id"], "2020-01-01T10:00:00.000000+00:00"))
    assert in_app(app, service.expire) == 0
    db.execute("UPDATE site_settings SET draft_expiration_hours = 24")
    assert in_app(app, service.expire) == 1


def test_editor_panel_and_disabled_feature(app, client, make_user, login):
    page = make_page(app, "Panel")
    login(client, make_user("dr_panel", role="editor"))
    assert "data-drafts" in client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    set_feature(app, "drafts", False)
    assert "data-drafts" not in client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    assert _save(client, page).status_code == 404


def test_rename_rewrites_links_in_drafts(app, client, admin_client, db, admin):
    target = make_page(app, "Target")
    page = make_page(app, "Linker")
    _save(admin_client, page, "see [t](/page/target)")
    admin_client.post("/page/target/rename", data={"new_slug": "moved"})
    assert _draft(db, page, admin)["content"] == "see [t](/page/moved)"
    assert target
