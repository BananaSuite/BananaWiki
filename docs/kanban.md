# Kanban

BananaWiki includes a built-in Kanban board system for task and project
management. Boards contain columns, and columns contain tickets that can be
dragged, reordered, and moved between columns. The built-in **kanban** plugin
provides the feature.

## Overview

Boards are the top-level containers. Each board has a title, an optional
description, and a configurable visibility level. Columns are the vertical
lanes within a board ("To Do", "In Progress", "Done") and can be reordered by
drag-and-drop. Tickets are the individual work items within a column; a ticket
carries a title, a Markdown description, a priority level, an assignee, a due
date, a colour label, file attachments, change history, and threaded comments.

Every new board starts with three default columns: To Do, In Progress, and
Done.

## Access Control

Two layers govern Kanban access: global site settings that control who can see
and use Kanban at all, and per-board sharing rules that control access to
individual boards.

### Global access settings

Two site settings in **Admin -> Settings** control global Kanban access:

| Setting | Key | Options | Default | Description |
|---|---|---|---|---|
| Kanban Access | `kanban_access` | `admin`, `editor`, `all` | `admin` | Who can view Kanban boards globally |
| Kanban Write Access | `kanban_write_access` | `admin`, `editor`, `all` | `admin` | Who can create boards and modify content globally |

`admin` restricts Kanban to admins, `editor` opens it to admins and editors,
and `all` opens it to every logged-in user.

### Per-board visibility

Each board has a visibility setting:

| Visibility | Description |
|---|---|
| `private` | Only the board creator and explicitly shared users/roles can see the board. |
| `shared` | The board creator and explicitly shared users/roles can see the board. |
| `public` | Anyone with global Kanban access can see the board. |

### Individual user shares bypass global access

Users who are individually shared on a board can access that board even if they
do not have global Kanban access. This lets admins share specific boards with
users who would otherwise not see Kanban at all. Such a user sees only the
boards shared with them individually and the boards they created. `public`
boards stay hidden from them.

### Access check flow

1. No account gets any access without the `kanban.view` permission, and
   nobody gets any while the kanban plugin is disabled.
2. Admins always have full access to all boards.
3. Board creators always have full access to their own boards.
4. Users with an explicit user-specific share can access the board regardless
   of global settings.
5. Users with global Kanban access can access `public` boards and boards shared
   with their role.
6. Everyone else is denied access.

`user_can_view_kanban_board()` in `routes/kanban.py` implements this check.
Every read path uses it: the board page, tickets, comments, attachments,
ticket and board history, the activity log, realtime sync, the board embeds
under `/api/embed/kanban/` and the personal data export. None of them shows
more than `/kanban/<board_id>` does. The database helper
`kanban_user_can_view_board()` treats every `public` board as visible, so it
is only correct for users who already have global access. Do not call it on
its own from route code.

Signed-out visitors are the one exception to this flow. In public mode with
`kanban_public_access_enabled` on, they can open the board list and the page
of each `public` board, read-only. Ticket details, comments, attachments,
history, the activity log, sync and the embeds stay closed to them.

### Write access

Write access (editing the board title and description, editing tickets,
managing columns) is only ever granted on a board the user can view:

- Admins and board creators always have write access.
- Users with an explicit `write`-level share have write access.
- Users with global `kanban_write_access` have write access to the boards they
  can view. Global write access does not reach a `private` or `shared` board
  that the user cannot see.
- `view`-level shares grant read-only access.

Anyone who can view a board can comment on its tickets. Creating a board needs
global write access.

### Personal data export

A user's data export (**Settings -> Export**) contains a full copy of a board,
with every ticket, comment and attachment record, only when the user can view
that board at export time. For boards the user worked on but can no longer
open, for example after a share was removed, the export contains only the
user's own comments, history entries, activity, assignments and uploads.

## Board Management

### Creating a board

Users with global write access create boards from the Kanban list page
(`/kanban`). A board needs a title of at most 10,000 characters; the optional
description is truncated to 10,000 characters. The board starts with the three
default columns and visibility `public`, and the creating user becomes the
board owner.

### Editing a board

Users with write access to the board can update the title and description via
`POST /kanban/<board_id>/edit`. The same length limits apply.

### Deleting a board

Board owners and admins can delete a board. The delete cascades to all
columns, tickets, attachments, history entries, comments, and share rules, and
removes orphaned attachment files from disk.

## Columns

### Creating a column

Users with write access to a board can add columns via the API. A column needs
a title of at most 10,000 characters and gets a sort order that determines its
left-to-right position.

### Editing a column

Column titles can be updated via `PUT /api/kanban/columns/<column_id>`, subject
to the same 10,000 character limit.

### Reordering columns

Columns support drag-and-drop reorder. The client sends the new column order as
a list of column IDs to `POST /api/kanban/<board_id>/columns/reorder`, and the
server updates all `sort_order` values in a single transaction.

### Deleting a column

The delete cascades to all tickets within the column and removes orphaned
attachment files from disk.

## Tickets

### Creating a ticket

Users with write access can create tickets in any column. A ticket needs a
title of at most 10,000 characters. The Markdown description is optional and
truncated to 10,000 characters. Priority is one of `low`, `medium` (default),
`high`, or `critical`. The assignee is an optional user ID, which must belong
to a user with board access. Sort order sets the position within the column.

### Editing a ticket

The following fields can be updated via `PUT /api/kanban/tickets/<id>`:

| Field | Type | Notes |
|---|---|---|
| `title` | text | Required, max 10,000 characters |
| `description` | text | Markdown, truncated to 10,000 characters. Changes are tracked in history. |
| `priority` | enum | `low`, `medium`, `high`, `critical` |
| `assigned_to` | user ID or null | Validated to have board access |
| `due_date` | ISO date or null | |
| `color` | hex colour or empty | Validated as a hex colour code |

### Moving a ticket

Tickets can be moved to a different column and reordered within a column via
`POST /api/kanban/tickets/<id>/move`. The request specifies the target column ID
and new sort order.

### Reordering tickets

Within a column, tickets support drag-and-drop reorder via
`POST /api/kanban/columns/<column_id>/tickets/reorder`.

### Deleting a ticket

The delete also removes the ticket's attachments, history entries, comments,
and orphaned attachment files on disk.

### Bulk actions

`POST /api/kanban/<board_id>/tickets/bulk` applies one action to a list of
tickets: `assign`, `priority`, `move`, `delete`, `color`, or `due_date`. Every
ticket must belong to the named board. `POST /api/kanban/<board_id>/columns/bulk`
does the same for columns and currently supports only `delete`.

## Ticket Attachments

Tickets support file attachments with the same security model as page
attachments.

### Configuration

| Setting | Value | Source |
|---|---|---|
| Storage folder | `instance/kanban_attachments/` | `config.KANBAN_ATTACHMENT_FOLDER` (overridable via `BW_KANBAN_ATTACHMENT_FOLDER`) |
| Max file size | 100 MB | `config.KANBAN_MAX_ATTACHMENT_SIZE` |
| Allowed extensions | `pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `txt`, `md`, `csv`, `json`, `xml`, `zip`, `tar`, `gz`, `png`, `jpg`, `jpeg`, `gif`, `webp`, `mp4`, `webm`, `mp3`, `ogg`, `py`, `js`, `ts`, `html`, `css`, `sh` | `config.KANBAN_ATTACHMENT_ALLOWED_EXTENSIONS` |

### Upload

Files are uploaded via `POST /api/kanban/tickets/<id>/attachments`
(rate-limited: 20 requests per 60 seconds). Files are stored with UUID
filenames to prevent collisions and path traversal attacks.

### Download

Files are downloaded via `GET /api/kanban/attachments/<id>/download`. The
original filename is preserved in the `Content-Disposition` header, and the
response is always sent as a download. Images, audio, video and plain text
keep their real `Content-Type`, so an image embedded in a ticket description
still shows. Every other type, including `.js`, `.css`, `.html` and `.svg`, is
sent as `application/octet-stream`. With the global `nosniff` header, a browser
then refuses to run an uploaded file as a script or apply it as a stylesheet on
the wiki origin.

### Delete

Attachments can be deleted via `DELETE /api/kanban/attachments/<id>`
(rate-limited: 20 requests per 60 seconds). The physical file is removed from
disk.

## Ticket History

The Kanban system tracks changes to ticket descriptions. Every time a ticket's
description is updated, the old and new content are saved as a history entry.

### Viewing history

`GET /api/kanban/tickets/<id>/history` returns all history entries for a
ticket, newest first. Each entry includes the old description text, the new
description text, the user who made the change, and the timestamp.

### Viewing a single entry with diff

`GET /api/kanban/history/<entry_id>` returns a single history entry with a
computed HTML diff showing what changed.

## Ticket Comments

Tickets support threaded comments for discussion.

### Adding a comment

`POST /api/kanban/tickets/<id>/comments` adds a new comment of at most 2,000
characters. Rate-limited: 30 requests per 60 seconds.

### Listing comments

`GET /api/kanban/tickets/<id>/comments` returns all comments for a ticket,
oldest first, with author names and timestamps.

### Editing a comment

`PUT /api/kanban/comments/<id>` updates a comment's content. Only the comment
author or an admin can edit. Rate-limited: 30 requests per 60 seconds.

### Deleting a comment

`DELETE /api/kanban/comments/<id>` removes a comment. Only the comment author
or an admin can delete. Rate-limited: 20 requests per 60 seconds.

Comments support Markdown rendering for display.

## Board Sharing

A board can be shared with a role or with an individual user.

### Role-based shares

Share a board with all users of a specific role (all editors, all admins).
Only roles that have global Kanban access can be targets of role-based shares.

| `share_type` | `target` | `access_level` | Effect |
|---|---|---|---|
| `role` | `admin` | `view` | All admins can view the board |
| `role` | `editor` | `write` | All editors can view and edit the board |
| `role` | `user` | `view` | All users can view the board |

### User-specific shares

Share a board with an individual user. User-specific shares bypass global
access restrictions: a user who does not have global Kanban access can still
reach boards shared with them individually.

| `share_type` | `target` | `access_level` | Effect |
|---|---|---|---|
| `user` | `<user_id>` | `view` | The specific user can view the board |
| `user` | `<user_id>` | `write` | The specific user can view and edit the board |

### Access levels

| Level | Can view | Can edit tickets | Can manage columns | Can manage sharing |
|---|---|---|---|---|
| `view` | Yes | No | No | No |
| `write` | Yes | Yes | Yes | No |
| Owner/Admin | Yes | Yes | Yes | Yes |

### Managing shares

Shares are managed via `POST /kanban/<board_id>/share` with actions:
`add_user`, `add_role`, `remove_user`, `remove_role`, `set_visibility`.

The API endpoint `PUT /api/kanban/<board_id>/settings` replaces all share rules
and the visibility setting in a single request.

### Assignee validation

When global access settings change or board shares are modified, the system
clears `assigned_to` on tickets whose assignee has lost board access
(`kanban_remove_assignees_without_board_access()` and
`kanban_remove_all_invalid_assignees()`).

## Database Tables

### `kanban_boards`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `title` | TEXT NOT NULL | Board title |
| `description` | TEXT | Default `''` |
| `created_by` | TEXT NOT NULL | FK to `users(id)` ON DELETE CASCADE |
| `created_at` | TEXT NOT NULL | ISO datetime |
| `visibility` | TEXT NOT NULL | `'private'`, `'shared'`, or `'public'` (default `'public'`) |

### `kanban_columns`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `board_id` | INTEGER NOT NULL | FK to `kanban_boards(id)` ON DELETE CASCADE |
| `title` | TEXT NOT NULL | Column title |
| `sort_order` | INTEGER NOT NULL | Default `0` |
| `created_at` | TEXT NOT NULL | ISO datetime |

### `kanban_tickets`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `column_id` | INTEGER NOT NULL | FK to `kanban_columns(id)` ON DELETE CASCADE |
| `title` | TEXT NOT NULL | Ticket title |
| `description` | TEXT | Default `''` |
| `priority` | TEXT NOT NULL | `'low'`, `'medium'`, `'high'`, or `'critical'` (default `'medium'`) |
| `assigned_to` | TEXT | FK to `users(id)` ON DELETE SET NULL |
| `created_by` | TEXT NOT NULL | FK to `users(id)` ON DELETE CASCADE |
| `sort_order` | INTEGER NOT NULL | Default `0` |
| `created_at` | TEXT NOT NULL | ISO datetime |
| `due_date` | TEXT | Optional ISO date |
| `color` | TEXT | Optional hex colour code (default `''`) |

### `kanban_board_shares`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `board_id` | INTEGER NOT NULL | FK to `kanban_boards(id)` ON DELETE CASCADE |
| `share_type` | TEXT NOT NULL | `'role'` or `'user'` |
| `target` | TEXT NOT NULL | Role name or user ID |
| `access_level` | TEXT NOT NULL | `'view'` or `'write'` (default `'view'`) |
| `created_at` | TEXT NOT NULL | ISO datetime |

Unique constraint: `(board_id, share_type, target)`

### `kanban_ticket_attachments`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ticket_id` | INTEGER NOT NULL | FK to `kanban_tickets(id)` ON DELETE CASCADE |
| `filename` | TEXT NOT NULL | UUID filename on disk |
| `original_name` | TEXT NOT NULL | Original upload filename |
| `file_size` | INTEGER NOT NULL | File size in bytes (default `0`) |
| `uploaded_by` | TEXT | FK to `users(id)` ON DELETE SET NULL |
| `uploaded_at` | TEXT NOT NULL | ISO datetime |

### `kanban_ticket_history`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ticket_id` | INTEGER NOT NULL | FK to `kanban_tickets(id)` ON DELETE CASCADE |
| `old_description` | TEXT | Previous description (default `''`) |
| `new_description` | TEXT | New description (default `''`) |
| `changed_by` | TEXT | FK to `users(id)` ON DELETE SET NULL |
| `created_at` | TEXT NOT NULL | ISO datetime |

### `kanban_ticket_comments`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `ticket_id` | INTEGER NOT NULL | FK to `kanban_tickets(id)` ON DELETE CASCADE |
| `user_id` | TEXT NOT NULL | FK to `users(id)` ON DELETE CASCADE |
| `content` | TEXT | Comment text (default `''`) |
| `created_at` | TEXT NOT NULL | ISO datetime |
| `updated_at` | TEXT NOT NULL | ISO datetime |

## Routes Reference

`routes/kanban.py` registers every route below in `register_kanban_routes()`.
Read that module when you need the authoritative list.

### HTML routes

| Method | Route | Description |
|---|---|---|
| GET | `/kanban` | List all boards the user can access |
| POST | `/kanban/create` | Create a new board |
| GET | `/kanban/<board_id>` | View a board with all columns and tickets |
| POST | `/kanban/<board_id>/edit` | Update board title and description |
| POST | `/kanban/<board_id>/delete` | Delete a board and all its contents |
| POST | `/kanban/<board_id>/share` | Manage board sharing rules |
| GET | `/kanban/<board_id>/export` | Download the board as JSON (10 per minute; every request counts, including browser downloads) |
| POST | `/kanban/import` | Create a board from an exported JSON file |
| GET | `/kanban/<board_id>/history` | Board revision history |
| GET | `/kanban/<board_id>/history/<entry_id>` | View one board history entry |
| POST | `/kanban/<board_id>/revert/<entry_id>` | Revert the board to a history entry |
| POST | `/kanban/<board_id>/history/<entry_id>/delete` | Delete one board history entry |
| POST | `/kanban/<board_id>/history/clear` | Clear the board history |

### API routes: Boards

| Method | Route | Description |
|---|---|---|
| POST | `/api/kanban/board-order` | Save the board ordering for the current user, or globally when `kanban_open_access` is on |
| GET | `/api/kanban/list-order-version` | Current `list_order_version`, polled by clients to detect reordering |
| GET | `/api/kanban/<board_id>/activity` | Recent activity log for a board (up to 50 entries) |
| GET | `/api/kanban/<board_id>/sync` | Realtime board events with `seq` greater than `since` |

### API routes: Columns

| Method | Route | Description |
|---|---|---|
| POST | `/api/kanban/<board_id>/columns` | Create a new column |
| PUT | `/api/kanban/columns/<column_id>` | Update column title |
| DELETE | `/api/kanban/columns/<column_id>` | Delete a column and its tickets |
| POST | `/api/kanban/<board_id>/columns/reorder` | Reorder columns |
| POST | `/api/kanban/<board_id>/columns/bulk` | Bulk column action (`delete`) |

### API routes: Tickets

| Method | Route | Description |
|---|---|---|
| POST | `/api/kanban/columns/<column_id>/tickets` | Create a new ticket |
| GET | `/api/kanban/tickets/<ticket_id>` | Get ticket details (with rendered description) |
| PUT | `/api/kanban/tickets/<ticket_id>` | Update ticket fields |
| DELETE | `/api/kanban/tickets/<ticket_id>` | Delete a ticket |
| POST | `/api/kanban/tickets/<ticket_id>/move` | Move ticket to a different column |
| POST | `/api/kanban/columns/<column_id>/tickets/reorder` | Reorder tickets within a column |
| POST | `/api/kanban/<board_id>/tickets/bulk` | Bulk ticket action (`assign`, `priority`, `move`, `delete`, `color`, `due_date`) |

### API routes: Board Settings & Sharing

| Method | Route | Description |
|---|---|---|
| GET | `/api/kanban/<board_id>/settings` | Get board visibility, shares, and access settings |
| PUT | `/api/kanban/<board_id>/settings` | Update board visibility and replace all shares |

### API routes: Attachments

| Method | Route | Description | Rate Limit |
|---|---|---|---|
| GET | `/api/kanban/tickets/<ticket_id>/attachments` | List ticket attachments | - |
| POST | `/api/kanban/tickets/<ticket_id>/attachments` | Upload an attachment | 20/60s |
| DELETE | `/api/kanban/attachments/<attachment_id>` | Delete an attachment | 20/60s |
| GET | `/api/kanban/attachments/<attachment_id>/download` | Download an attachment | - |

### API routes: History

| Method | Route | Description |
|---|---|---|
| GET | `/api/kanban/tickets/<ticket_id>/history` | List ticket description history |
| GET | `/api/kanban/history/<entry_id>` | Get a single history entry with diff |

### API routes: Comments

| Method | Route | Description | Rate Limit |
|---|---|---|---|
| GET | `/api/kanban/tickets/<ticket_id>/comments` | List ticket comments | - |
| POST | `/api/kanban/tickets/<ticket_id>/comments` | Add a comment | 30/60s |
| PUT | `/api/kanban/comments/<comment_id>` | Edit a comment (author/admin) | 30/60s |
| DELETE | `/api/kanban/comments/<comment_id>` | Delete a comment (author/admin) | 20/60s |

The `@_kanban_access_required` decorator protects every route above,
including the attachment download route. It checks `kanban.view` and lets
through users with global access or at least one individual share. Each
handler then checks the specific board with `user_can_view_kanban_board()` (or
the write rules above). The one exception is
`GET /api/kanban/list-order-version`, which is rate-limited only.

The board embeds used by wiki pages, `GET /api/embed/kanban/<board_id>` and
`GET /api/embed/kanban/<board_id>/sync`, live in `routes/api.py` and apply the
same per-board read check.

## Configuration

### Site settings (Admin -> Settings)

| Setting | Default | Description |
|---|---|---|
| `kanban_access` | `admin` | Global Kanban view access: `admin`, `editor`, or `all` |
| `kanban_write_access` | `admin` | Global Kanban write access: `admin`, `editor`, or `all` |

### Application configuration (`config.py`)

| Setting | Env Override | Default | Description |
|---|---|---|---|
| `KANBAN_ATTACHMENT_FOLDER` | `BW_KANBAN_ATTACHMENT_FOLDER` | `instance/kanban_attachments` | Storage directory for ticket attachments |
| `KANBAN_MAX_ATTACHMENT_SIZE` | - | 100 MB (`100 * 1024 * 1024`) | Maximum file size per attachment |
| `KANBAN_ATTACHMENT_ALLOWED_EXTENSIONS` | - | See list below | Permitted file extensions |

### Allowed attachment extensions

```
pdf, doc, docx, xls, xlsx, ppt, pptx,
txt, md, csv, json, xml, zip, tar, gz,
png, jpg, jpeg, gif, webp,
mp4, webm, mp3, ogg,
py, js, ts, html, css, sh
```
