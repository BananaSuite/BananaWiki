"""Test badge system functionality."""

import os
import sys
import tempfile

# Add the parent directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def test_badge_creation():
    """Test creating badge types."""
    print("Testing badge type creation...")

    # Create first_edit badge
    badge_id = db.create_badge_type(
        name="First Contributor",
        description="Made your first edit to the wiki",
        icon="✏️",
        color="#4a90e2",
        enabled=True,
        auto_trigger=True,
        trigger_type="first_edit",
        trigger_threshold=1
    )
    assert badge_id > 0, "Badge creation failed"
    print(f"  ✓ Created badge with ID: {badge_id}")

    # Verify badge exists
    badge = db.get_badge_type(badge_id)
    assert badge is not None, "Badge not found"
    assert badge['name'] == "First Contributor"
    assert badge['trigger_type'] == "first_edit"
    print(f"  ✓ Badge retrieved successfully: {badge['icon']} {badge['name']}")

    # Create contribution count badge
    badge_id_2 = db.create_badge_type(
        name="Prolific Contributor",
        description="Made 10 contributions to the wiki",
        icon="🌟",
        color="#ffd700",
        enabled=True,
        auto_trigger=True,
        trigger_type="contribution_count",
        trigger_threshold=10
    )
    print(f"  ✓ Created second badge with ID: {badge_id_2}")

    # List all badges
    badges = db.list_badge_types()
    assert len(badges) >= 2, "Not all badges were created"
    print(f"  ✓ Total badges: {len(badges)}")


def test_badge_awarding():
    """Test awarding badges to users."""
    print("\nTesting badge awarding...")

    # Create a test user
    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")
    print(f"  ✓ Created test user with ID: {user_id}")

    # Create a badge to award (each test runs with a fresh DB)
    db.create_badge_type(
        name="First Contributor",
        description="Made your first edit to the wiki",
        icon="✏️",
        color="#4a90e2",
        enabled=True,
        auto_trigger=True,
        trigger_type="first_edit",
        trigger_threshold=1
    )

    # Get a badge to award
    badges = db.list_badge_types()
    badge = badges[0]

    # Award badge to user
    result = db.award_badge(user_id, badge['id'], awarded_by=None)
    assert result is not None, "Badge awarding failed"
    print(f"  ✓ Awarded badge '{badge['name']}' to user")

    # Verify user has badge
    user_badges = db.get_user_badges(user_id)
    assert len(user_badges) > 0, "User doesn't have awarded badge"
    assert user_badges[0]['name'] == badge['name']
    print(f"  ✓ User has {len(user_badges)} badge(s)")

    # Test duplicate awarding (should return None)
    result = db.award_badge(user_id, badge['id'], awarded_by=None)
    assert result is None, "Duplicate badge was awarded"
    print("  ✓ Duplicate badge awarding prevented")


def test_badge_revocation():
    """Test revoking badges."""
    print("\nTesting badge revocation...")

    # Set up: create user and badge, then award it
    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")
    badge_id = db.create_badge_type(
        name="First Contributor",
        description="Made your first edit",
        icon="✏️",
        color="#4a90e2",
        enabled=True,
        auto_trigger=False,
        trigger_type="first_edit",
        trigger_threshold=1
    )
    db.award_badge(user_id, badge_id, awarded_by=None)

    # Get test user and their badges
    user = db.get_user_by_username("testuser")
    user_badges = db.get_user_badges(user['id'])
    badge = user_badges[0]

    # Revoke badge
    db.revoke_badge(user['id'], badge['badge_type_id'], revoked_by=None, permanent=False)
    print("  ✓ Revoked badge temporarily")

    # Verify badge is revoked
    active_badges = db.get_user_badges(user['id'], include_revoked=False)
    assert len(active_badges) == 0, "Badge still active after revocation"
    print("  ✓ Badge no longer active")

    # Verify badge still exists in history
    all_badges = db.get_user_badges(user['id'], include_revoked=True)
    assert len(all_badges) > 0, "Badge was permanently deleted"
    assert all_badges[0]['revoked'] == 1, "Badge not marked as revoked"
    print("  ✓ Badge kept in history as revoked")


def test_auto_triggers():
    """Test automatic badge triggering."""
    print("\nTesting automatic badge triggers...")

    # Set up: create user and a first_edit badge
    from werkzeug.security import generate_password_hash
    db.create_user("testuser", generate_password_hash("password"), role="user")
    db.create_badge_type(
        name="First Contributor",
        description="Made your first edit",
        icon="✏️",
        color="#4a90e2",
        enabled=True,
        auto_trigger=True,
        trigger_type="first_edit",
        trigger_threshold=1
    )

    # Get test user
    user = db.get_user_by_username("testuser")

    # Simulate making a page edit by creating a page
    from datetime import datetime
    page_id = db.create_page(
        title="Test Page",
        slug="test-page",
        content="Test content",
        user_id=user['id']
    )
    print(f"  ✓ Created test page with ID: {page_id}")

    # Check and award auto badges
    awarded = db.check_and_award_auto_badges(user['id'])
    print(f"  ✓ Auto-check completed, awarded: {awarded}")

    # Verify first_edit badge was awarded
    user_badges = db.get_user_badges(user['id'], include_revoked=False)
    first_edit_badges = [b for b in user_badges if b['name'] == 'First Contributor']
    assert first_edit_badges, "the first_edit trigger should have awarded the badge"


def test_badge_notifications():
    """Test badge notification system."""
    print("\nTesting badge notifications...")

    # Set up: create user and badge, then award it
    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")
    badge_id = db.create_badge_type(
        name="First Contributor",
        description="Made your first edit",
        icon="✏️",
        color="#4a90e2",
        enabled=True,
        auto_trigger=False,
        trigger_type="first_edit",
        trigger_threshold=1
    )
    db.award_badge(user_id, badge_id, awarded_by=None)

    user = db.get_user_by_username("testuser")

    # Get unnotified badges
    unnotified = db.get_unnotified_badges(user['id'])
    print(f"  ✓ User has {len(unnotified)} unnotified badge(s)")

    if len(unnotified) > 0:
        # Mark as notified
        db.mark_badges_notified(user['id'])
        print("  ✓ Marked badges as notified")

        # Verify no unnotified badges remain
        unnotified_after = db.get_unnotified_badges(user['id'])
        assert len(unnotified_after) == 0, "Badges still unnotified"
        print("  ✓ No unnotified badges remaining")


def test_allow_multiple_badge_awarding():
    """Test that badges with allow_multiple=True can be awarded multiple times."""
    print("\nTesting allow_multiple badge awarding...")

    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")

    # Create a badge with allow_multiple=True
    badge_id = db.create_badge_type(
        name="Contributor",
        description="Awarded for each contribution",
        icon="⭐",
        color="#ffd700",
        enabled=True,
        auto_trigger=False,
        trigger_type="",
        trigger_threshold=0,
        allow_multiple=True,
    )

    # Award the badge twice
    result1 = db.award_badge(user_id, badge_id, awarded_by=None)
    assert result1 is not None, "First award failed"
    print(f"  ✓ First award succeeded, id={result1}")

    result2 = db.award_badge(user_id, badge_id, awarded_by=None)
    assert result2 is not None, "Second award failed (allow_multiple should permit this)"
    assert result2 != result1, "Second award should create a new row"
    print(f"  ✓ Second award succeeded, id={result2}")

    # User should now have two active instances of this badge
    user_badges = db.get_user_badges(user_id, include_revoked=False)
    same_type = [b for b in user_badges if b['badge_type_id'] == badge_id]
    assert len(same_type) == 2, f"Expected 2 badge rows, got {len(same_type)}"
    print(f"  ✓ User has {len(same_type)} instances of the badge")

    # Award a third time to be thorough
    result3 = db.award_badge(user_id, badge_id, awarded_by=None)
    assert result3 is not None, "Third award failed"
    user_badges = db.get_user_badges(user_id, include_revoked=False)
    same_type = [b for b in user_badges if b['badge_type_id'] == badge_id]
    assert len(same_type) == 3, f"Expected 3 badge rows, got {len(same_type)}"
    print(f"  ✓ Third award succeeded; user now has {len(same_type)} instances")


def test_allow_multiple_no_duplicate_unread_notifications():
    """allow_multiple badges must not create duplicate unread notification rows."""
    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")

    badge_id = db.create_badge_type(
        name="Repeat Badge",
        description="Can be earned many times",
        icon="🔁",
        color="#00bfff",
        enabled=True,
        auto_trigger=False,
        trigger_type="",
        trigger_threshold=0,
        allow_multiple=True,
    )

    # Award the badge three times without marking notifications as read
    db.award_badge(user_id, badge_id)
    db.award_badge(user_id, badge_id)
    db.award_badge(user_id, badge_id)

    # There should be exactly one unread notification row, not three
    conn = db.get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM badge_notifications "
            "WHERE user_id=? AND badge_type_id=? AND notified=0",
            (user_id, badge_id),
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1, f"Expected 1 unread notification, got {count}"

    # After marking as notified, a new award should create a fresh notification
    db.mark_badges_notified(user_id)
    db.award_badge(user_id, badge_id)

    conn = db.get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM badge_notifications "
            "WHERE user_id=? AND badge_type_id=? AND notified=0",
            (user_id, badge_id),
        ).fetchone()[0]
    finally:
        conn.close()

    assert count == 1, f"Expected 1 new unread notification after mark, got {count}"


def test_allow_multiple_false_prevents_duplicates():
    """Test that badges with allow_multiple=False still prevent duplicate awards."""
    print("\nTesting allow_multiple=False still prevents duplicates...")

    from werkzeug.security import generate_password_hash
    user_id = db.create_user("testuser", generate_password_hash("password"), role="user")

    badge_id = db.create_badge_type(
        name="Unique Badge",
        description="Can only be earned once",
        icon="🏆",
        color="#4a90e2",
        enabled=True,
        auto_trigger=False,
        trigger_type="",
        trigger_threshold=0,
        allow_multiple=False,
    )

    result1 = db.award_badge(user_id, badge_id, awarded_by=None)
    assert result1 is not None, "First award failed"

    result2 = db.award_badge(user_id, badge_id, awarded_by=None)
    assert result2 is None, "Duplicate award should be prevented when allow_multiple=False"
    print("  ✓ Duplicate award correctly prevented for allow_multiple=False badge")

    user_badges = db.get_user_badges(user_id, include_revoked=False)
    same_type = [b for b in user_badges if b['badge_type_id'] == badge_id]
    assert len(same_type) == 1, f"Expected 1 badge row, got {len(same_type)}"
    print("  ✓ User has exactly 1 instance of the badge")


def test_schema_no_unique_constraint_on_user_badges():
    """Verify the user_badges table does not have UNIQUE(user_id, badge_type_id)."""
    import db as db_mod
    conn = db_mod.get_db()
    try:
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_badges'"
        ).fetchone()
        assert schema_row is not None, "user_badges table not found"
        assert "UNIQUE(user_id, badge_type_id)" not in schema_row[0], (
            "user_badges still has UNIQUE(user_id, badge_type_id) constraint"
        )
    finally:
        conn.close()


def test_badge_create_error_hides_db_details(logged_in_admin, monkeypatch):
    """Badge creation DB errors must not leak internal details to the user."""
    import db as db_mod

    def _boom(*a, **kw):
        raise RuntimeError("UNIQUE constraint failed: badge_types.name")

    monkeypatch.setattr(db_mod, "create_badge_type", _boom)

    resp = logged_in_admin.post("/admin/badges/create", data={
        "name": "TestBadge",
        "description": "desc",
        "icon": "🏆",
        "color": "#ffd700",
        "trigger_type": "first_edit",
        "trigger_threshold": "1",
    }, follow_redirects=True)

    assert b"An error occurred while saving the badge" in resp.data
    assert b"UNIQUE constraint" not in resp.data


def test_badge_update_error_hides_db_details(logged_in_admin, monkeypatch):
    """Badge update DB errors must not leak internal details to the user."""
    import db as db_mod

    badge_id = db_mod.create_badge_type(
        name="ExistingBadge", description="d", icon="🏆",
        color="#ffd700", trigger_type="first_edit", trigger_threshold=1,
    )

    def _boom(*a, **kw):
        raise RuntimeError("UNIQUE constraint failed: badge_types.name")

    monkeypatch.setattr(db_mod, "update_badge_type", _boom)

    resp = logged_in_admin.post(f"/admin/badges/{badge_id}/edit", data={
        "action": "update",
        "name": "Changed",
        "description": "d",
        "icon": "🏆",
        "color": "#ffd700",
        "trigger_type": "first_edit",
        "trigger_threshold": "1",
    }, follow_redirects=True)

    assert b"An error occurred while saving the badge" in resp.data
    assert b"UNIQUE constraint" not in resp.data


def main():
    """Standalone runner for badge tests."""
    print("=" * 60)
    print("BADGE SYSTEM TESTS")
    print("=" * 60)

    db.init_db()
    print("✓ Database initialized\n")

    try:
        test_badge_creation()
        test_badge_awarding()
        test_badge_revocation()
        test_auto_triggers()
        test_badge_notifications()
        test_allow_multiple_badge_awarding()
        test_allow_multiple_false_prevents_duplicates()
        test_schema_no_unique_constraint_on_user_badges()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
