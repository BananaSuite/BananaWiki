# Temporary Items

> Temporary item management is available at **Admin → Temporary Items**
> (`/admin/temporary`).  This is a core feature with no plugin gate.

## Overview

BananaWiki supports four kinds of time-limited items:

| Item type | What happens on expiry |
|---|---|
| **Temporary pages** | Page is permanently deleted. |
| **Temporary user accounts** | User account is permanently deleted. |
| **Temporary role grants** | User's role reverts to its original value. |
| **Temporary page index states** | Page's `is_deindexed` flag is restored to its prior value. |

All expiry times are stored and evaluated in UTC.  Admins set expiry dates
through the admin panel; the system handles cleanup automatically.

---

## Temporary Pages

Assign an expiry date to any wiki page.  When the expiry time passes the page
and all of its history, attachments, and drafts are deleted.

| Field | Description |
|---|---|
| `page_id` | The wiki page to schedule for deletion. |
| `expires_at` | UTC ISO datetime when the page will be removed. |
| `show_countdown` | If enabled, a visible countdown is displayed on the page. |

**Use cases:** time-limited announcements, seasonal content, event-specific
pages.

---

## Temporary User Accounts

Schedule a user account for automatic deletion.  On expiry the user and all
associated data are removed.

| Field | Description |
|---|---|
| `user_id` | The user account to schedule for deletion. |
| `expires_at` | UTC ISO datetime when the account will be removed. |
| `show_countdown` | If enabled, the user sees a countdown in the UI. |

**Safety guards:**

- Owner accounts cannot be scheduled, and an existing schedule never deletes
  an owner.
- The last remaining admin account is never auto-deleted, even if scheduled.
- Admins cannot schedule their own account, and superuser accounts cannot be
  scheduled.

**Use cases:** guest accounts, temporary collaborators, contractor access with
a known end date.

---

## Temporary Role Grants

Temporarily raise (or change) a user's role.  When the grant expires the
user's role automatically reverts to the stored `original_role`.

| Field | Description |
|---|---|
| `user_id` | The user whose role is temporarily changed. |
| `original_role` | Role to revert to on expiry: `user`, `editor` or `admin`, and lower than the user's current role. |
| `expires_at` | UTC ISO datetime when the role reverts. |
| `show_countdown` | If enabled, the user sees a countdown in the UI. |

**Safety guards:**

- Owner and superuser accounts cannot be given a temporary role, and admins
  cannot set or remove a schedule on their own account.
- While a schedule exists, the user's role cannot be changed from the user
  list, and an admin with a schedule cannot turn on owner status from
  **Settings**. Another admin removes the schedule first. The temporary role
  routes refuse owner accounts, so once the user is an owner nobody can
  remove the schedule, and the expiry would still take owner status away.
- On expiry the role reverts to `original_role` even if the user is an owner
  by then, so owner status never makes a temporary grant permanent.
- The last admin is never demoted if that would leave no admins.

**Use cases:** giving a user temporary editor access for a review cycle,
short-term admin privileges for a deployment.

---

## Temporary Page Index States

Temporarily change whether a page appears in the sidebar and search results.
On expiry the page's `is_deindexed` flag is restored to the value it held
before the temporary state was applied.

| Field | Description |
|---|---|
| `page_id` | The wiki page whose index state is temporarily changed. |
| `target_state` | The deindex state to apply now (0 = indexed, 1 = deindexed). |
| `restore_state` | The deindex state to restore on expiry. |
| `expires_at` | UTC ISO datetime when the index state reverts. |
| `show_countdown` | If enabled, a countdown is shown on the page. |

**Use cases:** temporarily hiding a page during an audit, temporarily surfacing
a deindexed page for a limited window.

---

## Automatic Cleanup

`cleanup_all_expired_temporary()` cleans up expired temporary items. The
periodic background cleanup runs it every five minutes, and it also runs when
an admin visits `/admin/temporary`.

The cleanup function processes all four item types in a single pass and returns
a summary dict:

```python
{
    "expired_temp_pages": int,
    "expired_temp_users": int,
    "expired_temp_roles": int,
    "expired_temp_page_index_states": int
}
```

Cleanup is idempotent. Running it multiple times with no newly expired items
produces zero changes.

---

## Restrictions and Edge Cases

- A page or user can have only one active temporary schedule at a time
  (enforced by `UNIQUE` constraints on `page_id` / `user_id`).
- Removing a temporary schedule before expiry cancels it: the item is kept.
- If a user is deleted by other means before their temporary schedule expires,
  the orphaned schedule row is harmlessly ignored during cleanup.
- Owners are never deleted automatically. The last remaining admin is
  never deleted or demoted automatically, even if a schedule exists. Other
  owners and admins are demoted on expiry like anyone else (see Temporary
  Role Grants).
- All timestamps are UTC.  The admin form should present times in the site
  timezone but they are converted to UTC for storage.

---

## Routes

All routes require `@admin_required`. The user and role routes refuse owner
accounts and the acting admin's own account. Superuser accounts cannot be
scheduled for deletion or given a temporary role.

| Method | Path | Description |
|---|---|---|
| GET | `/admin/temporary` | Main management page (lists all active schedules, runs cleanup) |
| POST | `/admin/temporary/page` | Set or update a page expiry |
| POST | `/admin/temporary/page/<page_id>/remove` | Remove a page expiry (cancel deletion) |
| POST | `/admin/temporary/user` | Set or update a user account expiry |
| POST | `/admin/temporary/user/<user_id>/remove` | Remove a user account expiry |
| POST | `/admin/temporary/role` | Set or update a temporary role grant |
| POST | `/admin/temporary/role/<user_id>/remove` | Remove a temporary role grant |

---

## Database Tables

### `temp_pages`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `page_id` | INTEGER UNIQUE FK | References `pages.id` |
| `expires_at` | TEXT NOT NULL | UTC ISO datetime |
| `show_countdown` | INTEGER | 0 or 1 (default 1) |
| `set_by` | TEXT FK | References `users.id` (admin who set the schedule) |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |

### `temp_users`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `user_id` | TEXT UNIQUE FK | References `users.id` |
| `expires_at` | TEXT NOT NULL | UTC ISO datetime |
| `show_countdown` | INTEGER | 0 or 1 (default 1) |
| `set_by` | TEXT FK | References `users.id` |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |

### `temp_roles`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `user_id` | TEXT UNIQUE FK | References `users.id` |
| `original_role` | TEXT CHECK | `user`, `editor`, `admin`, or `owner` |
| `expires_at` | TEXT NOT NULL | UTC ISO datetime |
| `show_countdown` | INTEGER | 0 or 1 (default 1) |
| `set_by` | TEXT FK | References `users.id` |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |

### `temp_page_index_state`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `page_id` | INTEGER UNIQUE FK | References `pages.id` |
| `target_state` | INTEGER | 0 (indexed) or 1 (deindexed): the state to apply now |
| `restore_state` | INTEGER | 0 or 1: the state to restore on expiry |
| `expires_at` | TEXT NOT NULL | UTC ISO datetime |
| `show_countdown` | INTEGER | 0 or 1 (default 1) |
| `set_by` | TEXT FK | References `users.id` |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |
