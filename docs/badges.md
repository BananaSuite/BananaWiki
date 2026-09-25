# Badges

> **Plugin:** `badges` (must be enabled in **Admin → Plugins**).

## Overview

BananaWiki includes an achievement badge system.  Admins create badge types
with names, descriptions, icons, and colours.  Badges can be awarded
automatically when a user meets a trigger condition, or manually by an admin.

---

## Badge Types

Each badge type defines:

| Field | Description |
|---|---|
| `name` | Unique display name. |
| `description` | Short explanation shown to users. |
| `icon` | Emoji icon (default `🏆`). |
| `color` | Hex colour code (default `#ffd700`). |
| `enabled` | Whether the badge is active and can be earned. |
| `auto_trigger` | If enabled, the badge is awarded automatically when the trigger fires. |
| `trigger_type` | One of the five auto-trigger types (see below), or empty for manual-only. |
| `trigger_threshold` | Numeric threshold for count-based triggers. |
| `allow_multiple` | Whether a user can earn the same badge more than once. |

---

## Auto-Trigger Types

BananaWiki supports five automatic trigger types.  When auto-trigger is enabled
for a badge, `db.check_and_award_auto_badges(user_id)` evaluates the
conditions on login and after page creates/edits.

| Trigger type | Fires when | Threshold meaning |
|---|---|---|
| `first_edit` | The user creates or edits their very first wiki page. | Ignored (threshold = 0). |
| `contribution_count` | The user's total page create + edit count reaches the threshold. | Number of contributions required (e.g. 10, 50, 100). |
| `category_count` | The user has contributed to at least N distinct categories. | Number of distinct categories required (e.g. 5). |
| `member_days` | The user's account age reaches N days. | Number of days since account creation (e.g. 365). |

Badges with `allow_multiple = 0` are only awarded once per user.  Badges with
`allow_multiple = 1` can be re-earned (useful for repeatable achievements).

---

## Manual Award and Revoke

Admins can award any badge to any user, or revoke a previously awarded badge,
from **Admin → Badges → (badge) → Award / Revoke**.

- **Award:** select a user and click Award.  A notification is created.
- **Revoke:** select a user-badge entry and click Revoke.  Sets `revoked = 1`
  and records the revoking admin and timestamp.
- **Revoke all:** remove a badge from every user who holds it.

Revoked badges remain in the database for audit purposes but are no longer
displayed on the user's profile.

---

## Badge Notifications

When a badge is awarded (automatically or manually) a row is inserted into the
`badge_notifications` table with `notified = 0`.  The template context
processor injects `badge_notification_count` into every page so the UI can
display an unread badge indicator.

Users can view their pending notifications at `/badges/notifications` and
dismiss them, which sets `notified = 1`.

Duplicate unread notifications for the same badge type and user are prevented,
so `allow_multiple` badges do not spam users with notifications.

---

## Default Badge Seeding

Run the seed script to populate the database with seven sensible defaults:

```bash
python scripts/seed_badges.py
```

| Badge | Icon | Colour | Trigger | Threshold |
|---|---|---|---|---|
| First Edit | ✏️ | `#4a90e2` | `first_edit` | - |
| Prolific Contributor | 🌟 | `#ffd700` | `contribution_count` | 10 |
| Super Contributor | 💫 | `#ff6b6b` | `contribution_count` | 50 |
| Master Contributor | ⭐ | `#9b59b6` | `contribution_count` | 100 |
| Diverse Contributor | 🌈 | `#3498db` | `category_count` | 5 |
| Veteran | 🎖️ | `#2ecc71` | `member_days` | 365 |

The seed script is idempotent and skips badges that already exist (matched by
name).

---

## Admin Management

From **Admin → Badges** admins can:

- **Create** a new badge type with custom icon, colour, trigger, and threshold.
- **Edit** an existing badge type's properties.
- **Delete** a badge type (removes all awarded instances and notifications).
- **Award** a badge to a specific user.
- **Revoke** a badge from a specific user.
- **Revoke all**: remove a badge from every user.

---

## Routes

### Admin Routes (require `@admin_required`)

| Method | Path | Description |
|---|---|---|
| GET | `/admin/badges` | List all badge types |
| POST | `/admin/badges/create` | Create a new badge type |
| GET | `/admin/badges/<badge_id>/edit` | Edit form for a badge type |
| POST | `/admin/badges/<badge_id>/edit?action=update` | Update a badge type |
| POST | `/admin/badges/<badge_id>/edit?action=delete` | Delete a badge type |
| POST | `/admin/badges/<badge_id>/award` | Award badge to a user |
| POST | `/admin/badges/<badge_id>/revoke` | Revoke badge from a user |
| POST | `/admin/badges/<badge_id>/revoke-all` | Revoke badge from all users |

### User-Facing Routes (require `@login_required`)

| Method | Path | Description |
|---|---|---|
| GET | `/badges/notifications` | View pending badge notifications |
| POST | `/badges/notifications/dismiss` | Mark notifications as read |

---

## Database Tables

### `badge_types`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `name` | TEXT UNIQUE | Badge display name |
| `description` | TEXT | User-visible description |
| `icon` | TEXT | Emoji icon (default `🏆`) |
| `color` | TEXT | Hex colour (default `#ffd700`) |
| `enabled` | INTEGER | 0 or 1 (default 1) |
| `auto_trigger` | INTEGER | 0 = manual only, 1 = automatic |
| `trigger_type` | TEXT CHECK | `first_edit`, `contribution_count`, `category_count`, `member_days`, or empty |
| `trigger_threshold` | INTEGER | Numeric threshold for count-based triggers |
| `allow_multiple` | INTEGER | 0 or 1 (default 0) |
| `created_by` | TEXT FK | References `users.id` |
| `created_at` | TEXT | UTC ISO timestamp |

### `user_badges`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `user_id` | TEXT FK | References `users.id` |
| `badge_type_id` | INTEGER FK | References `badge_types.id` |
| `earned_at` | TEXT | UTC ISO timestamp |
| `awarded_by` | TEXT FK | References `users.id` (NULL for auto-awarded) |
| `revoked` | INTEGER | 0 or 1 (default 0) |
| `revoked_at` | TEXT | UTC ISO timestamp (NULL if not revoked) |
| `revoked_by` | TEXT FK | References `users.id` |

### `badge_notifications`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `user_id` | TEXT FK | References `users.id` |
| `badge_type_id` | INTEGER FK | References `badge_types.id` |
| `notified` | INTEGER | 0 = unread, 1 = dismissed (default 0) |
| `created_at` | TEXT | UTC ISO timestamp |
