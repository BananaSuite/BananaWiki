"""
Seed the database with default badge types.

This script creates a set of commonly-used badge types that can be
enabled/disabled or modified by admins.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


DEFAULT_BADGES = [
    {
        "name": "First Edit",
        "description": "Made your first contribution to the wiki",
        "icon": "✏️",
        "color": "#4a90e2",
        "auto_trigger": True,
        "trigger_type": "first_edit",
        "trigger_threshold": 0,
    },
    {
        "name": "Prolific Contributor",
        "description": "Made 10 contributions to the wiki",
        "icon": "🌟",
        "color": "#ffd700",
        "auto_trigger": True,
        "trigger_type": "contribution_count",
        "trigger_threshold": 10,
    },
    {
        "name": "Super Contributor",
        "description": "Made 50 contributions to the wiki",
        "icon": "💫",
        "color": "#ff6b6b",
        "auto_trigger": True,
        "trigger_type": "contribution_count",
        "trigger_threshold": 50,
        "allow_multiple": False,
    },
    {
        "name": "Master Contributor",
        "description": "Made 100 contributions to the wiki",
        "icon": "⭐",
        "color": "#9b59b6",
        "auto_trigger": True,
        "trigger_type": "contribution_count",
        "trigger_threshold": 100,
        "allow_multiple": False,
    },
    {
        "name": "Diverse Contributor",
        "description": "Contributed to 5 different categories",
        "icon": "🌈",
        "color": "#3498db",
        "auto_trigger": True,
        "trigger_type": "category_count",
        "trigger_threshold": 5,
    },
    {
        "name": "Veteran",
        "description": "Member for 365 days",
        "icon": "🎖️",
        "color": "#2ecc71",
        "auto_trigger": True,
        "trigger_type": "member_days",
        "trigger_threshold": 365,
    },
]


def seed_badges(enabled=False):
    """Create default badge types in the database.

    Args:
        enabled: If True, badges are enabled by default. If False, they're disabled
                 and admins must manually enable them.
    """
    db.init_db()

    created_count = 0
    skipped_count = 0

    print("Seeding default badge types...")
    print("-" * 60)

    for badge_data in DEFAULT_BADGES:
        existing = db.get_badge_type_by_name(badge_data["name"])
        if existing:
            print(f"⊘ Skipped '{badge_data['name']}' (already exists)")
            skipped_count += 1
            continue

        badge_id = db.create_badge_type(
            name=badge_data["name"],
            description=badge_data["description"],
            icon=badge_data["icon"],
            color=badge_data["color"],
            enabled=enabled,
            auto_trigger=badge_data.get("auto_trigger", False),
            trigger_type=badge_data.get("trigger_type", ""),
            trigger_threshold=badge_data.get("trigger_threshold", 0),
            allow_multiple=badge_data.get("allow_multiple", False),
        )
        print(f"✓ Created badge #{badge_id}: {badge_data['icon']} {badge_data['name']}")
        created_count += 1

    print("-" * 60)
    print(f"Created: {created_count} badge(s)")
    print(f"Skipped: {skipped_count} badge(s)")
    print("\nNote: All badges are created in DISABLED state by default.")
    print("Admins can enable them via /admin/badges")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed default badge types")
    parser.add_argument("--enabled", action="store_true",
                       help="Create badges in enabled state (default: disabled)")
    args = parser.parse_args()

    seed_badges(enabled=args.enabled)
