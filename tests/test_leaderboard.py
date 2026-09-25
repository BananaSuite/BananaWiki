from werkzeug.security import generate_password_hash

import db


def _create_editor(username):
    return db.create_user(username, generate_password_hash("pw"), role="editor")


def test_leaderboard_stats_include_rich_contributor_metrics(isolated_db):
    alice_id = _create_editor("alice")
    bob_id = _create_editor("bob")

    alpha_id = db.create_page("Alpha", "leaderboard-alpha", "abc", user_id=alice_id)
    db.update_page(alpha_id, "Alpha", "abcdef", bob_id, "expand")
    db.update_page(alpha_id, "Alpha", "ab", alice_id, "trim")
    beta_id = db.create_page("Beta", "leaderboard-beta", "hello world", user_id=alice_id)
    db.update_page(beta_id, "Beta", "hello world!", alice_id, "punctuation", is_revert=True)

    result = db.get_leaderboard_stats(sort_by="score", limit=10)
    entries = {entry["username"]: entry for entry in result["entries"]}

    alice = entries["alice"]
    bob = entries["bob"]

    assert alice["edit_count"] == 4
    assert alice["pages_touched"] == 2
    assert alice["pages_created"] == 2
    assert alice["revert_count"] == 1
    assert alice["added"] >= 15
    assert alice["deleted"] >= 4
    assert alice["avg_change"] > 0
    assert bob["edit_count"] == 1
    assert bob["pages_touched"] == 1
    assert result["summary"]["contributor_count"] == 2
    assert result["summary"]["edit_count"] == 5
    assert result["summary"]["pages_touched"] == 2
    assert result["podium"][0]["rank"] == 1


def test_leaderboard_page_renders_podium_and_rich_columns(logged_in_admin):
    alice_id = _create_editor("alice")
    bob_id = _create_editor("bob")
    cara_id = _create_editor("cara")
    db.update_site_settings(contributor_leaderboard_enabled=1)

    alpha_id = db.create_page("Alpha", "leaderboard-route-alpha", "alpha body", user_id=alice_id)
    db.update_page(alpha_id, "Alpha", "alpha body expanded", alice_id, "expand alpha")
    beta_id = db.create_page("Beta", "leaderboard-route-beta", "beta", user_id=bob_id)
    db.update_page(beta_id, "Beta", "beta plus", bob_id, "expand beta")
    db.create_page("Cara", "leaderboard-route-cara", "c", user_id=cara_id)

    response = logged_in_admin.get("/leaderboard?sort=chars")

    assert response.status_code == 200
    assert b"leaderboard-podium-first" in response.data
    assert b"Podium" in response.data
    assert b"Pages touched" in response.data
    assert b"Avg/edit" in response.data
    assert b"alice" in response.data
    assert b"bob" in response.data
    assert b"cara" in response.data
