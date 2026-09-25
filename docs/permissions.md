# Permissions

BananaWiki combines a four-tier role hierarchy with custom roles,
per-user permission overrides, and per-category access controls.

## Role Hierarchy

BananaWiki has four built-in roles arranged from least to most privileged.
Admins and protected admins have all permissions regardless of custom
permission settings (except when gated by a disabled plugin).

| Role | Display Label | Description |
|---|---|---|
| `user` | Member | Read-only access. Can view pages, profiles, and search. Cannot create or edit content. |
| `editor` | Editor | Read + write access. Can create/edit pages, manage categories, upload files, and use chat. Granular per-category access can restrict which categories an editor can read or write. |
| `admin` | Administrator | Full access to everything including user management, site settings, announcements, badges, migration, audit, and moderation tools. |
| `owner` | Administrator | Identical to admin, but immune to modification by other admins: cannot be suspended, demoted, or have their role changed. |

### Role precedence

- Protected admins cannot be suspended, role-changed, or de-attributed by
  any other user (including other protected admins via the web UI).
- Admins can manage all non-protected-admin users.
- All permission checks for admins and protected admins return `True`
  unconditionally (unless the permission is gated by a disabled plugin).

## Custom Roles

Admins can create **custom roles**, reusable permission bundles that can
be assigned to multiple users at once. Custom roles let admins manage
permissions for groups of users without configuring each user
individually.

### How custom roles work

1. Each custom role has a **base role** (`user` or `editor`) that
   determines which permissions are assignable.
2. The role defines a specific set of **enabled permissions** chosen from
   the permissions available to its base role.
3. The role can optionally restrict **category read access** and
   **category write access** to specific categories.
4. Assigning a custom role to a user replaces any per-user permission
   overrides, and the role's permissions take effect.

### Managing custom roles

Create a role at Admin -> Roles -> Create New Role. Edit it at Admin -> Roles
-> (click role); changes propagate to all assigned users. Assign it at
Admin -> Roles -> (click role) -> Assign to User. Unassigning removes the
custom role from a user and assigns the best matching replacement role, or
falls back to defaults.

Deleting a role finds the most similar remaining role (Jaccard similarity of
permission keys plus a base-role bonus) and reassigns affected users. If no
similar role exists, those users receive the default permissions for their
base role.

### Custom role database tables

- `custom_roles`: role definitions (name, description, base_role,
  category access restrictions, timestamps)
- `custom_role_permissions`: permission keys assigned to each role
- `custom_role_categories`: category access rules per role
- `users.custom_role_id`: foreign key linking a user to their custom role

## Permission Reference

BananaWiki defines 39 permissions across 11 categories. Each
permission has a unique dot-notation key, a human-readable label, a
description, and default values for editors and users.

### Page Permissions (7)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `page.view_all` | View All Pages | View all pages including deindexed ones | Yes | Yes |
| `page.view_deindexed` | View Deindexed Pages | View pages marked as deindexed | Yes | No |
| `page.create` | Create Pages | Create new wiki pages | Yes | No |
| `page.edit_all` | Edit All Pages | Edit any page in allowed categories | Yes | No |
| `page.delete` | Delete Pages | Delete wiki pages | No | No |
| `page.edit_metadata` | Edit Page Metadata | Change page title, slug, category | Yes | No |
| `page.deindex` | Deindex Pages | Mark pages as deindexed/hidden | No | No |

Deleting a page needs `page.delete` on top of edit access to the page, both
on the web and through the API (`DELETE /api/v1/pages/<slug>`). Editors do
not get it by default, so the page's Delete button only appears for users
who hold it.

### Category Permissions (6)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `category.view_all` | View All Categories | View all category pages | Yes | Yes |
| `category.create` | Create Categories | Create new categories | Yes | No |
| `category.edit` | Edit Categories | Rename and modify categories | Yes | No |
| `category.delete` | Delete Categories | Delete categories | Yes | No |
| `category.reorder` | Reorder Categories | Change category sort order | Yes | No |
| `category.manage_sequential` | Manage Sequential Navigation | Enable/disable sequential navigation in categories | Yes | No |

Deleting a category needs `category.delete` and write access to that
category. Deleting it together with its pages also needs `page.delete`,
and the category dialog only offers that choice to users who hold it. Each
page then goes through the same checks as a single delete: a protected
page, a page reserved by someone else or a page with an auto-deletion
schedule stops the whole deletion, and with the `deletion_slowdown` plugin
enabled the pages are queued for deletion instead of being removed at once.

### Page History Permissions (4)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `history.view` | View History | View page edit history | Yes | Yes |
| `history.revert` | Revert Changes | Revert pages to previous versions | No | No |
| `history.delete` | Delete History | Delete individual history entries | No | No |
| `history.transfer` | Transfer Attribution | Transfer page attribution to another user | No | No |

### Draft Permissions (4)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `draft.create` | Create Drafts | Save page drafts | Yes | No |
| `draft.view_own` | View Own Drafts | View your own drafts | Yes | No |
| `draft.delete_own` | Delete Own Drafts | Delete your own drafts | Yes | No |
| `draft.transfer` | Transfer Drafts | Transfer drafts to other users | No | No |

### File Attachment Permissions (4)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `attachment.upload` | Upload Files | Upload files attached to pages | Yes | No |
| `attachment.view` | View Attachments | View and download attachments | Yes | Yes |
| `attachment.delete_own` | Delete Own Attachments | Delete attachments you uploaded | Yes | No |
| `attachment.delete_any` | Delete Any Attachment | Delete any user's attachments | No | No |

The attachment routes check these permissions:

- `attachment.upload` for uploading page attachments. Images uploaded into
  the page text through `/api/upload` follow the upload settings instead.
- `attachment.view` for downloading one attachment and for "Download all".
- `attachment.delete_own` for deleting an attachment the user uploaded.
- `attachment.delete_any` for deleting anyone's attachment. Editors do not
  have it by default, so a default editor can only delete their own files.

The page view and the editor hide the Add file, Download and Delete
controls a user is not allowed to use.

### Page Tags Permissions (2)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `tag.edit_difficulty` | Edit Difficulty Tags | Set difficulty tags on pages | Yes | No |
| `tag.edit_custom` | Edit Custom Tags | Set custom tags on pages | Yes | No |

### User Profiles Permissions (2)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `profile.view` | View User Profiles | View public user profiles | Yes | Yes |
| `profile.edit_own` | Edit Own Profile | Edit your own profile page | Yes | Yes |

### Chat & Messaging Permissions (4)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `chat.dm` | Direct Messages | Send and receive direct messages | Yes | Yes |
| `chat.group` | Group Chats | Join and participate in group chats | Yes | Yes |
| `chat.create_group` | Create Group Chats | Create new group chats | No | No |
| `chat.upload` | Upload Chat Attachments | Upload files in chats | Yes | Yes |

### Search & Navigation Permissions (2)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `search.pages` | Search Pages | Search wiki content | Yes | Yes |
| `search.users` | Search Users | Search for users | Yes | Yes |

### Invite Code Permissions (3)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `invite.generate` | Generate Invite Codes | Create invite codes for new users | No | No |
| `invite.view` | View Invite Codes | View list of invite codes | No | No |
| `invite.delete` | Delete Invite Codes | Delete unused invite codes | No | No |

### PDF Export Permissions (1)

| Key | Label | Description | Editor Default | User Default |
|---|---|---|---|---|
| `page.export_pdf` | Export Pages as PDF | Download wiki pages and history entries as PDF files | No | No |

## Default Permissions by Role

### Admin / Protected Admin

All 39 permissions are granted unconditionally. The only exception is
when a permission is gated by a disabled plugin (see
[Plugin-Gated Permissions](#plugin-gated-permissions)).

### Editor (23 permissions enabled by default)

`page.view_all`, `page.view_deindexed`, `page.create`, `page.edit_all`,
`page.edit_metadata`, `category.view_all`, `category.create`,
`category.edit`, `category.delete`, `category.reorder`,
`category.manage_sequential`, `history.view`, `draft.create`,
`draft.view_own`, `draft.delete_own`, `attachment.upload`,
`attachment.view`, `attachment.delete_own`, `tag.edit_difficulty`,
`tag.edit_custom`, `profile.view`, `profile.edit_own`, `chat.dm`,
`chat.group`, `chat.upload`, `search.pages`, `search.users`

### User / Member (11 permissions enabled by default)

`page.view_all`, `category.view_all`, `history.view`,
`attachment.view`, `profile.view`, `profile.edit_own`, `chat.dm`,
`chat.group`, `chat.upload`, `search.pages`, `search.users`

## Editor-Only Permissions

The following 25 permissions can only be assigned to editors (or admins).
They are never assignable to users, even through custom roles or
per-user overrides:

`page.create`, `page.edit_all`, `page.delete`, `page.edit_metadata`,
`page.deindex`, `category.create`, `category.edit`, `category.delete`,
`category.reorder`, `category.manage_sequential`, `history.revert`,
`history.delete`, `history.transfer`, `draft.create`, `draft.view_own`,
`draft.delete_own`, `draft.transfer`, `attachment.upload`,
`attachment.delete_own`, `attachment.delete_any`, `tag.edit_difficulty`,
`tag.edit_custom`, `invite.generate`, `invite.view`, `invite.delete`

## Admin-Only Permissions

`custom_page.manage` (create, edit and delete custom pages) belongs to
admins and owners only. It cannot be given to editors or users through a
custom role or a per-user override, and `has_permission()` refuses it for
those roles even when an old role row still lists it. A custom page is
served at any free path of the wiki, can redirect visitors to any site and
publishes its files to anonymous visitors, which is admin-level power.
The old `custom_page.view` permission no longer exists: published custom
pages are visible to everyone who can see the wiki.

## Permission Implications

Some permissions imply other permissions. Granting a permission also
grants its implied dependencies:

| Permission | Implies |
|---|---|
| `page.view_deindexed` | `page.view_all` |
| `page.create` | `page.view_all` |
| `page.edit_all` | `page.view_all` |
| `page.delete` | `page.view_all` |
| `page.edit_metadata` | `page.view_all` |
| `page.deindex` | `page.view_all` |
| `category.create` | `category.view_all` |
| `category.edit` | `category.view_all` |
| `category.delete` | `category.view_all` |
| `category.reorder` | `category.view_all` |
| `category.manage_sequential` | `category.view_all` |

For example, granting `page.create` to a user automatically grants
`page.view_all` as well. `normalize_permission_keys()` applies
implications transitively.

## Plugin-Gated Permissions

When a built-in plugin is disabled, all permissions gated by that plugin
are effectively revoked, even for users who would otherwise have them.
`has_permission()` checks plugin status before returning `True`.

| Plugin | Gated Permissions |
|---|---|
| **page_history** | `history.view`, `history.revert`, `history.delete`, `history.transfer`, `page.export_pdf` |
| **drafts** | `draft.create`, `draft.view_own`, `draft.delete_own`, `draft.transfer` |
| **attachments** | `attachment.upload`, `attachment.view`, `attachment.delete_own`, `attachment.delete_any` |
| **difficulty_tags** | `tag.edit_difficulty`, `tag.edit_custom` |
| **user_profiles** | `profile.view`, `profile.edit_own` |
| **chat** | `chat.dm`, `chat.group`, `chat.create_group`, `chat.upload` |

Permissions not listed here (pages, categories, search, invites) are
always active and not gated by any plugin.

## Category-Level Access

Beyond per-permission controls, editors and users can have **category-level
access restrictions** that limit which categories they can read or write.

### Read access

Unrestricted is the default: the user can view all categories. When read
access is restricted, only categories in the user's allowed read list are
visible, and pages in other categories are hidden from the sidebar, search
results, and direct access.

### Write access (editors only)

Unrestricted is the default: the editor can edit pages in all categories.
When write access is restricted, only categories in the editor's allowed
write list accept edits. The editor can still view other categories if read
access is unrestricted.

### Consistency rule

The system ensures editors never have less read access than write access.
If an editor has write access to a category, they automatically receive
read access to it as well (`_normalize_category_read_access()`).

### Uncategorized pages

If read access is restricted, uncategorized pages (those not assigned to
any category) are hidden by default.

## Per-User Permission Overrides

Admins can configure permissions on a per-user basis at
**Admin -> Users -> (select user) -> Permissions**.

### How it works

1. Navigate to **Admin -> Users** and select a user.
2. Click **Permissions** to open the permission editor.
3. Toggle individual permissions on/off.
4. Configure category read and write access restrictions.
5. Save: changes take effect immediately.

### Interaction with custom roles

A custom role's permissions take precedence over per-user overrides, and
assigning a custom role clears any existing per-user overrides. To use
per-user overrides, first unassign the custom role.

### Permission resolution order

1. Check if the permission is gated by a disabled plugin -> deny.
2. If the user is admin or owner -> allow.
3. Check if the user has a custom role -> use the role's permissions.
4. Fall back to per-user permission overrides.
5. Fall back to default permissions for the user's base role.

## Route Guard Decorators

Three decorators in `helpers/_auth.py` protect route handlers:

### `@login_required`

Ensures the user is logged in. Redirects to `/login` if not
authenticated. In public mode, allows unauthenticated visitors to
access read-only routes.

```python
@app.route("/wiki/<slug>")
@login_required
def view_page(slug):
    ...
```

### `@editor_required`

Ensures the user has the `editor`, `admin`, or `owner` role.
Users with the `user` role are redirected to the home page with an error
flash message.

```python
@app.route("/wiki/create", methods=["GET", "POST"])
@login_required
@editor_required
def create_page():
    ...
```

### `@admin_required`

Ensures the user has the `admin` or `owner` role. Non-admins
receive a 403 error (JSON response for API requests, HTML error page
otherwise).

```python
@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    ...
```

## Helper Functions

### `has_permission(user, permission_key)`

The primary permission check function. Returns `True` if the user has the
specified permission.

```python
from db import has_permission

if has_permission(current_user, "page.create"):
    # user can create pages
```

Resolution logic:

1. Returns `False` if the permission is gated by a disabled plugin.
2. Returns `True` unconditionally for admins and protected admins.
3. For editors and users, checks the custom role or per-user permissions.
4. Validates that the permission is assignable to the user's role.

### `editor_has_category_access(user, category_id)`

Checks whether an editor has **write** access to a specific category.
Admins always have access. Falls back to the legacy editor category
access system if no custom permissions are configured.

```python
from helpers import editor_has_category_access

if editor_has_category_access(current_user, category_id):
    # editor can write to this category
```

### `user_can_view_page(user, page)`

Checks whether a user can view a specific page, considering:

- Pending deletion status (only admins can view).
- Deindexed status (requires `page.view_deindexed` permission).
- Category read access restrictions.
- Public mode status (unauthenticated visitors in public mode).

```python
from helpers import user_can_view_page

if user_can_view_page(current_user, page):
    # user can see this page
```

### `user_can_view_category(user, category_id)`

Checks whether a user has read access to a specific category. Admins
always have access.

```python
from helpers import user_can_view_category

if user_can_view_category(current_user, category_id):
    # user can see pages in this category
```

### `filter_visible_navigation(categories, uncategorized, user)`

Filters the sidebar navigation to show only categories and pages the
user is allowed to see, based on visibility policy and category access
rules. Used by the template context processor to build the sidebar.

### `get_current_user()`

Returns the currently logged-in user row (from `session["user_id"]`) or
`None`. The result is cached per-request in `flask.g` to avoid repeated
database lookups.

## Database Schema

### `user_permissions` table

Stores per-user permission overrides. Each row grants a single permission
to a single user.

| Column | Type | Description |
|---|---|---|
| `user_id` | TEXT | FK to `users(id)` ON DELETE CASCADE |
| `permission_key` | TEXT | Permission key (e.g. `page.create`) |

Unique constraint: `(user_id, permission_key)`

### `user_category_access` table

Stores whether a user's category access (read or write) is restricted.

| Column | Type | Description |
|---|---|---|
| `user_id` | TEXT | FK to `users(id)` ON DELETE CASCADE |
| `access_type` | TEXT | `'read'` or `'write'` |
| `restricted` | INTEGER | `0` = unrestricted, `1` = restricted |

Unique constraint: `(user_id, access_type)`

### `user_allowed_categories` table

When access is restricted, lists the specific categories the user can
access.

| Column | Type | Description |
|---|---|---|
| `user_id` | TEXT | FK to `users(id)` ON DELETE CASCADE |
| `category_id` | INTEGER | FK to `categories(id)` ON DELETE CASCADE |
| `access_type` | TEXT | `'read'` or `'write'` |

Unique constraint: `(user_id, category_id, access_type)`

### `custom_roles` table

Stores admin-created custom role definitions.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key (autoincrement) |
| `name` | TEXT | Unique role name |
| `description` | TEXT | Optional description |
| `base_role` | TEXT | `'user'` or `'editor'` |
| `read_restricted` | INTEGER | `0` or `1` |
| `write_restricted` | INTEGER | `0` or `1` |
| `created_by` | TEXT | FK to `users(id)` |
| `created_at` | TEXT | ISO datetime |
| `updated_at` | TEXT | ISO datetime |

### `custom_role_permissions` table

Permission keys assigned to a custom role.

| Column | Type | Description |
|---|---|---|
| `role_id` | INTEGER | FK to `custom_roles(id)` ON DELETE CASCADE |
| `permission_key` | TEXT | Permission key |

Unique constraint: `(role_id, permission_key)`

### `custom_role_categories` table

Category access rules for a custom role.

| Column | Type | Description |
|---|---|---|
| `role_id` | INTEGER | FK to `custom_roles(id)` ON DELETE CASCADE |
| `category_id` | INTEGER | FK to `categories(id)` ON DELETE CASCADE |
| `access_type` | TEXT | `'read'` or `'write'` |

Unique constraint: `(role_id, category_id, access_type)`

### `users.custom_role_id` column

Foreign key linking a user to their assigned custom role. Set to `NULL`
when no custom role is assigned. `ON DELETE SET NULL` clears the column
if the role is deleted.

## Admin Interface

### User permissions

**Admin -> Users -> (select user) -> Permissions**

Configure per-user permission overrides, category read access, and
category write access for individual users.

### Editor access (legacy)

**Admin -> Users -> (select user) -> Editor Access**

Legacy interface for configuring editor-specific category write access.
Superseded by the custom permission system but still functional as a
fallback.

### Custom roles

| Route | Method | Description |
|---|---|---|
| `/admin/roles` | GET | List all custom roles with user counts |
| `/admin/roles/create` | GET, POST | Create a new custom role |
| `/admin/roles/<role_id>` | GET, POST | View and edit a custom role |
| `/admin/roles/<role_id>/assign` | POST | Assign the role to a user |
| `/admin/roles/<role_id>/unassign` | POST | Remove the role from a user |
| `/admin/roles/<role_id>/delete` | POST | Delete the role |
