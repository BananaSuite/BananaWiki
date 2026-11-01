"""Regression tests for the NW2 findings of the full re-audit pass 8 (categories, page builder, page
protection while impersonating and quota requests)."""

from __future__ import annotations

import html
import logging
import re

from bananawiki.wiki.features.pages import categories, navigation
from tests.features.api_support import call, enable_api, issue
from tests.features.pages_support import in_app, make_category, make_page

# NW2-1: categories nest at most MAX_DEPTH levels; deeper old trees still render


def _chain(app, length: int, parent: int | None = None) -> list[int]:
    ids = []
    for level in range(length):
        parent = make_category(app, f"level {level}", parent)["id"]
        ids.append(parent)
    return ids


def test_nw2_1_categories_cannot_nest_past_the_depth_bound(app, client, make_user, login, db, monkeypatch,
                                                           frozen_limiter_clock):
    monkeypatch.setattr(categories, "MAX_DEPTH", 4, raising=False)
    chain = _chain(app, 4)
    login(client, make_user("mallory", role="editor"))
    before = db.scalar("SELECT COUNT(*) FROM categories")
    assert client.post("/category/create", data={"name": "too deep", "parent_id": str(chain[-1])}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM categories") == before
    assert client.post("/category/create", data={"name": "fits", "parent_id": str(chain[-2])}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM categories") == before + 1
    # Moving a two-level subtree under level 3 would make it five levels deep.
    subtree = _chain(app, 2)
    assert client.post(f"/category/{subtree[0]}/move", data={"parent_id": str(chain[2])}).status_code == 302
    assert db.scalar("SELECT parent_id FROM categories WHERE id = ?", (subtree[0],)) is None
    assert client.post(f"/category/{subtree[0]}/move", data={"parent_id": str(chain[1])}).status_code == 302
    assert db.scalar("SELECT parent_id FROM categories WHERE id = ?", (subtree[0],)) == chain[1]
    # The REST API refuses the same, with a translated error.
    enable_api(app, db)
    token = issue(app, make_user("apieditor", role="editor", api_access_enabled=1), scopes=("categories",))
    api = app.test_client()
    created = call(api, "POST", "/categories", token, json={"name": "api deep", "parent_id": chain[-1]})
    assert created.status_code == 400 and created.get_json()["code"] == "category_too_deep"
    assert created.get_json()["error"] == "Categories can be nested at most 4 levels deep."
    moved = call(api, "PUT", f"/categories/{subtree[0]}", token, json={"parent_id": chain[3]})
    assert moved.status_code == 400 and moved.get_json()["code"] == "category_too_deep"


def test_nw2_1_a_refused_api_move_leaves_the_category_unrenamed(app, make_user, db, monkeypatch,
                                                                 frozen_limiter_clock):
    monkeypatch.setattr(categories, "MAX_DEPTH", 3, raising=False)
    chain = _chain(app, 3)
    loose = make_category(app, "loose")["id"]
    enable_api(app, db)
    token = issue(app, make_user("apieditor", role="editor", api_access_enabled=1), scopes=("categories",))
    api = app.test_client()
    moved = call(api, "PUT", f"/categories/{loose}", token,
                 json={"name": "renamed", "parent_id": chain[-1], "sequential_nav": True})
    assert moved.status_code == 400 and moved.get_json()["code"] == "category_too_deep"
    row = db.one("SELECT name, parent_id, sequential_nav FROM categories WHERE id = ?", (loose,))
    assert (row["name"], row["parent_id"], row["sequential_nav"]) == ("loose", None, 0)
    fits = call(api, "PUT", f"/categories/{loose}", token, json={"name": "renamed", "parent_id": chain[1]})
    assert fits.status_code == 200
    row = db.one("SELECT name, parent_id FROM categories WHERE id = ?", (loose,))
    assert (row["name"], row["parent_id"]) == ("renamed", chain[1])


def test_nw2_1_the_number_of_categories_is_bounded(app, client, make_user, login, db, monkeypatch,
                                                   frozen_limiter_clock):
    monkeypatch.setattr(categories, "MAX_CATEGORIES", db.scalar("SELECT COUNT(*) FROM categories") + 2, raising=False)
    login(client, make_user("mallory", role="editor"))
    for name in ("one", "two", "three"):
        assert client.post("/category/create", data={"name": name}).status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE name IN ('one', 'two', 'three')") == 2


def test_nw2_1_a_deep_old_tree_does_not_break_the_navigation(app, make_user, login, db, caplog):
    db.execute("UPDATE site_settings SET public_mode = 1 WHERE id = 1")
    make_page(app, "Marker", "x")
    # A 400-level chain left by an older version (inserted directly: creating it is refused now).
    parent = None
    for level in range(400):
        parent = db.insert("categories", {"name": f"deep {level}", "parent_id": parent, "sort_order": 0,
                                          "created_at": "2026-01-01 00:00:00"})
    make_page(app, "Bottom page", "x", category_id=parent)
    reader = app.test_client()
    login(reader, make_user("reader", role="user"))
    admin = app.test_client()
    login(admin, make_user("boss", role="admin"))
    for who in (app.test_client(), reader, admin):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            page = who.get("/page/marker")
            listing = who.get("/navigation")
        assert page.status_code == 200 and listing.status_code == 200
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert "/page/bottom-page" in page.get_data(as_text=True)
        assert "/page/bottom-page" in listing.get_data(as_text=True)
    # Below the bound the deepest categories hang from the last level that is shown.
    nav = in_app(app, lambda: navigation.tree(None))
    depth, nodes = 0, nav["categories"]
    while nodes:
        depth += 1
        nodes = [child for node in nodes for child in node["children"]]
    assert depth == categories.MAX_DEPTH
    old = in_app(app, lambda: categories.tree(None))
    depth, nodes = 0, old["categories"]
    while nodes:
        depth += 1
        nodes = [child for node in nodes for child in node["children"]]
    assert depth == categories.MAX_DEPTH and "Bottom page" in str(old)


def test_nw2_1_spawning_the_guide_at_the_category_bound_is_refused_not_a_500(app, make_user, login, db,
                                                                              monkeypatch):
    def full():
        monkeypatch.setattr(categories, "MAX_CATEGORIES", db.scalar("SELECT COUNT(*) FROM categories"),
                            raising=False)

    make_category(app, "Filler")
    full()
    admin = app.test_client()
    login(admin, make_user("boss", role="owner"))
    spawned = admin.post("/admin/spawn-docs", data={"variant": "simplified", "docs_language": "en"})
    assert spawned.status_code == 302
    assert db.scalar("SELECT docs_category_id FROM site_settings") is None
    refusal = "categories, the most it can hold"
    assert refusal in admin.get(spawned.headers["Location"]).get_data(as_text=True)
    # The onboarding wizard applies everything else and says why the guide is missing.
    owner = make_user("firstrun", role="owner", onboarding_required=1)
    wizard = app.test_client()
    login(wizard, owner)
    done = wizard.post("/onboarding", data={"site_name": "Team Wiki", "language": "en", "mode": "easy",
                                            "default_theme_mode": "light", "spawn_docs": "1",
                                            "docs_variant": "simplified", "docs_language": "en"})
    assert done.status_code == 302 and done.headers["Location"].endswith("/intro")
    assert db.scalar("SELECT onboarding_required FROM users WHERE id = ?", (owner["id"],)) == 0
    assert db.scalar("SELECT docs_category_id FROM site_settings") is None
    assert refusal in wizard.get(done.headers["Location"]).get_data(as_text=True)
    # Respawning a guide that exists replaces it, as its category makes room for the new one.
    monkeypatch.setattr(categories, "MAX_CATEGORIES", 10_000, raising=False)
    assert admin.post("/admin/spawn-docs", data={"variant": "simplified", "docs_language": "en"}).status_code == 302
    first = db.scalar("SELECT docs_category_id FROM site_settings")
    full()
    assert admin.post("/admin/spawn-docs", data={"variant": "full", "docs_language": "en"}).status_code == 302
    second = db.scalar("SELECT docs_category_id FROM site_settings")
    assert first and second and second != first
    assert db.scalar("SELECT COUNT(*) FROM categories WHERE name = 'BananaWiki'") == 1


def _sizes(clients, urls) -> list[int]:
    return [len(client.get(url).data) for client in clients for url in urls]


def test_nw2_1_a_deep_tree_costs_a_page_view_no_more_than_a_flat_one(app, make_user, login, db):
    # 24 chains of MAX_DEPTH categories with 100-character names: every
    # editor's page view lists each category's path in the move dialog, and
    # /navigation in the new-category form. Unshortened, those paths make a
    # deep tree's pages several times the size of the same categories side by side.
    with db.transaction():
        for chain in range(24):
            parent = None
            for level in range(categories.MAX_DEPTH):
                parent = db.insert("categories", {"name": f"{chain}-{level}-".ljust(categories.MAX_NAME, "x"),
                                                  "parent_id": parent, "sort_order": 0,
                                                  "created_at": "2026-01-01 00:00:00"})
    make_page(app, "Marker", "x")
    clients = []
    for role in ("editor", "admin"):
        clients.append(app.test_client())
        login(clients[-1], make_user(f"the{role}", role=role))
    urls = ("/page/marker", "/navigation")
    deep = _sizes(clients, urls)
    page = clients[0].get("/page/marker").get_data(as_text=True)
    picker = page[page.index('<select id="move-category"'):]
    options = re.findall(r"<option value=\"\d+\"[^>]*>([^<]*)</option>", picker[:picker.index("</select>")])
    assert len(options) == 24 * categories.MAX_DEPTH
    assert max(len(html.unescape(option)) for option in options) <= categories.MAX_PATH + 4
    db.execute("UPDATE categories SET parent_id = NULL")
    flat = _sizes(clients, urls)
    for deep_size, flat_size in zip(deep, flat, strict=True):
        assert deep_size < flat_size * 1.25, (deep, flat)


# NW2-2: builder publishes are limited per account like the Markdown editor's saves


def test_nw2_2_builder_publish_is_rate_limited(app, client, make_user, login, db, frozen_limiter_clock):
    db.execute("UPDATE site_settings SET page_builder_enabled = 1, page_builder_access = 'editor' WHERE id = 1")
    login(client, make_user("eve", role="editor"))
    make_page(app, "Target", "x")
    page_id = db.scalar("SELECT id FROM pages WHERE slug = 'target'")
    before = db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page_id,))
    codes = []
    for n in range(32):
        document = {"version": 2, "blocks": [{"type": "text", "format": "markdown", "text": f"version {n % 2}"}]}
        revision = db.scalar("SELECT revision FROM pages WHERE id = ?", (page_id,))
        codes.append(client.post("/api/page/target/builder/publish",
                                 json={"document": document, "base_revision": revision}).status_code)
    assert codes[:30] == [200] * 30 and codes[30:] == [429, 429]
    refused = client.post("/api/page/target/builder/publish", json={"document": {"version": 2, "blocks": []}})
    assert refused.get_json()["error"] == "Too many saves. Wait a minute and try again."
    assert db.scalar("SELECT COUNT(*) FROM page_history WHERE page_id = ?", (page_id,)) - before <= 30


# NW2-3: an administrator impersonating a protection's controller is bound like any administrator


def _protected_by_controller(app, make_user, login, db):
    from tests.features.pages_support import set_feature

    set_feature(app, "page_governance", True)
    set_feature(app, "audit", True)
    db.execute("UPDATE site_settings SET page_protection_enabled = 1 WHERE id = 1")
    page = make_page(app, "Guarded", "original")
    controller = make_user("ctrl", role="editor")
    browser = app.test_client()
    login(browser, controller)
    assert browser.post("/page/guarded/protection", data={"action": "protect"}).status_code == 302
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (page["id"],)) == controller["id"]
    admin = app.test_client()
    login(admin, make_user("padmin", role="admin"))
    return page, controller, admin


def test_nw2_3_impersonating_the_controller_neither_edits_nor_unprotects(app, make_user, login, db):
    page, controller, admin = _protected_by_controller(app, make_user, login, db)
    assert admin.post(f"/admin/users/{controller['id']}/impersonate").status_code == 302
    revision = db.scalar("SELECT revision FROM pages WHERE id = ?", (page["id"],))
    admin.post("/page/guarded/edit", data={"title": "Guarded", "content": "impersonated edit", "revision": revision})
    admin.post("/page/guarded/protection", data={"action": "unprotect"})
    admin.post("/page/guarded/delete")
    row = db.one("SELECT content, protected_by, pending_deletion FROM pages WHERE id = ?", (page["id"],))
    assert row is not None and row["content"] == "original" and row["protected_by"] == controller["id"]
    assert "impersonated edit" not in str(db.column("SELECT content FROM page_history WHERE page_id = ?",
                                                    (page["id"],)))
    # The page shows the protection as someone else's, with no button to lift it.
    shown = admin.get("/page/guarded").get_data(as_text=True)
    assert 'value="unprotect"' not in shown
    # Nor can the administrator protect another page in the controller's name.
    make_page(app, "Open page", "x")
    admin.post("/page/open-page/protection", data={"action": "protect"})
    assert db.scalar("SELECT protected_by FROM pages WHERE slug = 'open-page'") is None
    # The controller, signed in themselves, still can.
    own = app.test_client()
    login(own, controller)
    assert own.post("/page/guarded/protection", data={"action": "unprotect"}).status_code == 302
    assert db.scalar("SELECT protected_by FROM pages WHERE id = ?", (page["id"],)) is None


def test_nw2_3_page_changes_while_impersonating_are_audited(app, make_user, login, db):
    page, controller, admin = _protected_by_controller(app, make_user, login, db)
    make_page(app, "Open page", "x")
    padmin = db.scalar("SELECT id FROM users WHERE username = 'padmin'")
    assert admin.post(f"/admin/users/{controller['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    revision = db.scalar("SELECT revision FROM pages WHERE slug = 'open-page'")
    assert admin.post("/page/open-page/edit", data={"title": "Open page", "content": "in their name",
                                                    "revision": revision}).status_code == 302
    admin.post("/page/guarded/protection", data={"action": "unprotect"})
    admin.post("/api/preview", data={"content": "x"})
    rows = db.all("SELECT action, actor_id, target_type, target_id, details FROM audit_log WHERE id > ? ORDER BY id",
                  (mark,))
    assert [(r["action"], r["actor_id"], r["target_type"], r["target_id"]) for r in rows] == [
        ("page.impersonated_action", padmin, "page", "open-page"),
        ("page.impersonated_action", padmin, "page", "guarded"),
    ]
    assert '"as_user": "ctrl"' in rows[0]["details"] and '"endpoint": "pages.edit"' in rows[0]["details"]


def test_nw2_3_page_changes_through_other_features_are_audited(app, make_user, login, db):
    import re

    from bananawiki.wiki import permissions
    from bananawiki.wiki.features.contributions import service as contributions
    from bananawiki.wiki.features.pages import service as pages
    from tests.features.pages_support import restrict, set_feature

    for feature in ("audit", "contributions", "difficulty_tags", "assessments"):
        set_feature(app, feature, True)
    page = make_page(app, "Lesson", "original")
    proposer = make_user("proposer")
    reviewer = make_user("reviewer", role="editor")
    restrict(db, reviewer, keys=set(permissions.defaults("editor")) | {"contribution.review"})

    def propose():
        from tests.features.governance_support import as_user
        as_user(proposer)
        return contributions.propose(pages.get(page["id"]), proposer, title="Lesson", content="proposed",
                                     reason="typo")

    contribution_id = in_app(app, propose)
    admin = app.test_client()
    login(admin, make_user("padmin", role="admin"))
    padmin = db.scalar("SELECT id FROM users WHERE username = 'padmin'")
    assert admin.post(f"/admin/users/{reviewer['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    review = admin.get(f"/admin/contributions/{contribution_id}").get_data(as_text=True)
    form = {name: re.search(rf'name="{name}" value="([^"]*)"', review).group(1) for name in ("version", "page_revision")}
    assert admin.post(f"/admin/contributions/{contribution_id}/approve", data=form).status_code == 302
    assert db.scalar("SELECT content FROM pages WHERE id = ?", (page["id"],)) == "proposed"
    admin.post("/page/lesson/tag", data={"difficulty_tag": "expert"})
    admin.post("/page/lesson/assessment/delete")
    rows = db.all("SELECT actor_id, target_type, target_id, details FROM audit_log "
                  "WHERE id > ? AND action = 'page.impersonated_action' ORDER BY id", (mark,))
    assert [(r["actor_id"], r["target_type"], r["target_id"]) for r in rows] == [
        (padmin, "contribution", str(contribution_id)), (padmin, "page", "lesson"), (padmin, "page", "lesson")]
    endpoints = [r["details"] for r in rows]
    assert '"endpoint": "contributions.approve"' in endpoints[0] and '"as_user": "reviewer"' in endpoints[0]
    assert '"endpoint": "difficulty_tags.set_tag"' in endpoints[1]
    assert '"endpoint": "assessments.delete"' in endpoints[2]


def test_nw2_3_a_bulk_markdown_import_while_impersonating_is_audited(app, make_user, login, db):
    import io
    import zipfile

    from tests.features.pages_support import set_feature

    set_feature(app, "audit", True)
    make_page(app, "Target", "x")
    superuser = make_user("chief", role="owner", is_superuser=1)
    target = make_user("badmin", role="admin")
    browser = app.test_client()
    login(browser, superuser)
    assert browser.post(f"/admin/users/{target['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    # Reading a file into the editor changes no page, so it is not recorded.
    assert browser.post("/page/target/import-md", data={"import_file": (io.BytesIO(b"# T"), "t.md")},
                        content_type="multipart/form-data").status_code == 200
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Imported/new.md", "# Imported page\n\ntext")
    archive.seek(0)
    assert browser.post("/admin/bulk-markdown", data={"import_file": (archive, "bundle.zip")},
                        content_type="multipart/form-data").status_code == 302
    assert db.scalar("SELECT COUNT(*) FROM pages WHERE slug = 'imported-page'") == 1
    rows = db.all("SELECT actor_id, details FROM audit_log WHERE id > ? AND action = 'page.impersonated_action'",
                  (mark,))
    assert [row["actor_id"] for row in rows] == [superuser["id"]]
    assert '"endpoint": "page_export.bulk_import"' in rows[0]["details"]
    assert '"as_user": "badmin"' in rows[0]["details"]


def test_nw2_3_a_federated_copy_forked_while_impersonating_is_audited(app, make_user, login, db, monkeypatch):
    import time

    from bananawiki.wiki.features.federation import sync
    from tests.features.pages_support import set_feature

    set_feature(app, "audit", True)
    monkeypatch.setattr(sync, "active", lambda: True)  # BW_FEDERATION_ENABLED
    inbox = make_category(app, "Inbox")
    db.insert("federation_peers", {"wiki_id": "remote-wiki", "name": "Remote", "base_url": "http://127.0.0.1:9",
                                   "secret": "s", "audience_category": inbox["id"], "generation": "g",
                                   "last_success": time.time()})
    db.insert("federation_copies", {"peer_id": "remote-wiki", "page_id": "p1", "title": "Shared", "content": "text",
                                    "source_path": "/page/shared", "revision": "1"})
    superuser = make_user("chief", role="owner", is_superuser=1)
    target = make_user("fadmin", role="admin")
    browser = app.test_client()
    login(browser, superuser)
    assert browser.post(f"/admin/users/{target['id']}/impersonate").status_code == 302
    mark = db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log")
    assert browser.post("/federation/copies/remote-wiki/p1/fork").status_code == 302
    forked = db.one("SELECT * FROM pages WHERE slug LIKE 'federated-%'")
    assert forked and forked["last_edited_by"] == target["id"]
    assert browser.post("/admin/spawn-docs", data={"variant": "simplified", "docs_language": "en"}).status_code == 302
    rows = db.all("SELECT actor_id, target_type, target_id, details FROM audit_log "
                  "WHERE id > ? AND action = 'page.impersonated_action' ORDER BY id", (mark,))
    assert [(r["actor_id"], r["target_type"], r["target_id"]) for r in rows] == [
        (superuser["id"], "federation_copy", "remote-wiki/p1"), (superuser["id"], None, None)]
    assert '"endpoint": "federation.fork"' in rows[0]["details"] and '"as_user": "fadmin"' in rows[0]["details"]
    assert '"endpoint": "site_admin.spawn_docs"' in rows[1]["details"]


# NW2-4: an account files at most MAX_REQUESTS_PER_DAY quota requests in 24 hours


def _filed_and_cancelled(app, db, path_file, path_cancel, form, table, user, client):
    from tests.features.pages_support import set_feature

    for feature in ("contributions", "page_governance"):
        set_feature(app, feature, True)
    for n in range(15):
        client.post(path_file, data={**form, "requested_quota": str(10 + n), "reason": "R" * 2000})
        pending = db.scalar(f"SELECT id FROM {table} WHERE user_id = ? AND status = 'pending'", (user["id"],))
        client.post(path_cancel, data={"action": "cancel_request", "request_id": str(pending or 0)})
    return db.scalar(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user["id"],))


def test_nw2_4_contribution_quota_requests_are_bounded_per_day(app, client, make_user, login, db):
    user = make_user("plainu", role="user")
    login(client, user)
    filed = _filed_and_cancelled(app, db, "/my-contributions/quota-request", "/my-contributions/quota-request/cancel",
                                 {}, "contribution_quota_requests", user, client)
    assert filed == 10
    refused = client.post("/my-contributions/quota-request", data={"requested_quota": "50", "reason": "more"},
                          follow_redirects=True)
    assert "You have sent 10 requests in the last 24 hours." in refused.get_data(as_text=True)
    # A day later the account may ask again.
    db.execute("UPDATE contribution_quota_requests SET created_at = datetime(created_at, '-1 day')")
    client.post("/my-contributions/quota-request", data={"requested_quota": "50", "reason": "more"})
    assert db.scalar("SELECT COUNT(*) FROM contribution_quota_requests WHERE status = 'pending'") == 1


def test_nw2_4_reservation_quota_requests_are_bounded_per_day(app, client, make_user, login, db):
    user = make_user("editoru", role="editor")
    login(client, user)
    filed = _filed_and_cancelled(app, db, "/settings/reservation-quota", "/settings/reservation-quota",
                                 {"action": "submit_quota_request"}, "reservation_quota_requests", user, client)
    assert filed == 10
