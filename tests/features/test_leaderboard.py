"""Contributor leaderboard."""

from __future__ import annotations

import pytest

from .people_support import edit_page, in_app, make_category, make_page, restrict_read


@pytest.fixture
def enabled(db):
    db.execute("UPDATE site_settings SET contributor_leaderboard_enabled = 1")


def test_off_by_default(client, make_user, login):
    login(client, make_user("bob"))
    assert client.get("/leaderboard").status_code == 404


def test_requires_login(client, enabled):
    assert "/login" in client.get("/leaderboard").headers["Location"]


def test_sizes_are_cached_per_revision(app, make_user, db, enabled):
    from bananawiki.wiki.features.leaderboard import service

    bob = make_user("bob", role="editor")
    page = make_page(app, "Doc", "hello", author_id=bob["id"])
    edit_page(app, page["id"], "hello world", author_id=bob["id"])
    edit_page(app, page["id"], "hi", author_id=bob["id"])
    assert in_app(app, service.refresh) == 3
    rows = db.all("SELECT added, removed, is_first FROM leaderboard_revisions ORDER BY history_id")
    assert [(r["added"], r["removed"], r["is_first"]) for r in rows] == [(5, 0, 1), (6, 0, 0), (0, 9, 0)]
    assert in_app(app, service.refresh) == 0
    db.execute("DELETE FROM pages WHERE id = ?", (page["id"],))
    assert db.scalar("SELECT COUNT(*) FROM leaderboard_revisions") == 0


def test_ranking_and_page(app, client, make_user, login, enabled):
    bob, carol = make_user("bob", role="editor"), make_user("carol", role="editor")
    page = make_page(app, "Doc", "x" * 50, author_id=bob["id"])
    edit_page(app, page["id"], "x" * 60, author_id=bob["id"])
    make_page(app, "Other", "y", author_id=carol["id"])
    login(client, carol)
    html = client.get("/leaderboard").get_data(as_text=True)
    table = html[html.index("<tbody>"):]
    assert table.index("bob") < table.index("carol")
    assert "You are number 2" in html
    csv_text = client.get("/leaderboard/export?sort=edits").get_data(as_text=True)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("rank,username,score") and lines[1].split(",")[1] == "bob"


def test_hidden_pages_do_not_count_for_restricted_viewers(app, client, make_user, login, db, enabled):
    bob = make_user("bob", role="editor")
    open_cat, secret_cat = make_category(app, "Open"), make_category(app, "Secret")
    make_page(app, "Secret plans", "x", author_id=bob["id"], category_id=secret_cat["id"])
    viewer = make_user("eve")
    restrict_read(db, viewer, [open_cat["id"]])
    login(client, viewer)
    html = client.get("/leaderboard").get_data(as_text=True)
    assert "Secret plans" not in html and "bob" not in html


def test_date_range_does_not_count_old_content_as_added(app, client, make_user, login, db, enabled):
    bob = make_user("bob", role="editor")
    page = make_page(app, "Doc", "x" * 1000, author_id=bob["id"])
    db.execute("UPDATE page_history SET created_at = '2001-01-01 00:00:00'")
    edit_page(app, page["id"], "x" * 1010, author_id=bob["id"])
    login(client, bob)
    csv_text = client.get("/leaderboard/export?range=30d").get_data(as_text=True)
    row = dict(zip(csv_text.splitlines()[0].split(","), csv_text.splitlines()[1].split(","), strict=True))
    assert row["added"] == "10" and row["edit_count"] == "1" and row["pages_created"] == "0"


def test_ranking_is_kept_until_revisions_are_added_or_removed(app, make_user, db, enabled, monkeypatch):
    from bananawiki.wiki.features.leaderboard import service

    bob, carol = make_user("bob", role="editor"), make_user("carol", role="editor")
    page = make_page(app, "Doc", "hello", author_id=bob["id"])
    make_page(app, "Notes", "hi", author_id=carol["id"])
    computed: list[str] = []
    contributors = service._contributors

    def counted(viewer, range_key):
        computed.append(range_key)
        return contributors(viewer, range_key)

    monkeypatch.setattr(service, "_contributors", counted)

    def ranking(**options):
        return in_app(app, lambda: service.ranking(bob, **options))

    assert [e["username"] for e in ranking()["entries"]] == ["bob", "carol"]
    assert ranking(sort="edits")["summary"]["edits"] == 2 and computed == ["all"]
    db.execute("UPDATE users SET username = 'robert' WHERE id = ?", (bob["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (carol["id"],))
    result = ranking()
    assert [e["username"] for e in result["entries"]] == ["robert"] and result["summary"]["contributors"] == 1
    assert computed == ["all"]
    edit_page(app, page["id"], "hello world", author_id=bob["id"])
    assert ranking()["entries"][0]["edit_count"] == 2 and computed == ["all", "all"]
    ranking(range_key="30d")
    assert computed == ["all", "all", "30d"]


def test_latest_page_is_not_named_once_hidden_from_the_viewer(app, client, make_user, login, db, enabled):
    bob = make_user("bob", role="editor")
    open_cat, secret_cat = make_category(app, "Open"), make_category(app, "Secret")
    page = make_page(app, "Travel plans", "x", author_id=bob["id"], category_id=open_cat["id"])
    viewer = make_user("eve")
    restrict_read(db, viewer, [open_cat["id"]])
    login(client, viewer)
    assert "Travel plans" in client.get("/leaderboard").get_data(as_text=True)
    db.execute("UPDATE pages SET category_id = ? WHERE id = ?", (secret_cat["id"], page["id"]))
    assert "Travel plans" not in client.get("/leaderboard").get_data(as_text=True)
