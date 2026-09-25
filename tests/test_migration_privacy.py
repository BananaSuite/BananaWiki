"""Upgrades and ordinary worker starts must preserve administrator choices."""

import db
from db._canvas import create_layout, get_layout, list_layouts_for_user


def test_upgrade_keeps_private_canvases_and_explicit_site_preferences():
    with db.get_db_context() as conn:
        conn.execute("INSERT INTO users(id, username, password) VALUES ('owner', 'owner', 'hash')")
        conn.execute("INSERT INTO users(id, username, password) VALUES ('other', 'other', 'hash')")
        conn.execute("UPDATE site_settings SET new_user_intro_enabled=1, primary_color='#7c8dc6'")
        conn.commit()
    layout = create_layout("Private planning", "owner", visibility="private")
    with db.get_db_context() as conn:
        conn.execute("PRAGMA user_version=0")  # Import an installation from before the version ledger.
    db.init_db()
    db.init_db()  # Recycling a worker is not another migration.
    assert get_layout(layout)["visibility"] == "private"
    assert all(row["id"] != layout for row in list_layouts_for_user(db.get_user_by_id("other")))
    settings = db.get_site_settings()
    assert settings["new_user_intro_enabled"] == 1
    assert settings["primary_color"] == "#7c8dc6"
