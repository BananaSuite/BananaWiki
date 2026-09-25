# Canvas

> **Plugin:** `canvas` (must be enabled in **Admin → Plugins**).

## Overview

Canvas is a visual knowledge canvas for creating node-link diagrams directly
inside BananaWiki.  Each canvas layout is a JSON-backed graph of nodes and
edges that users can arrange, connect, and export.  Layouts can reference wiki
pages, external links, images, videos, and free-form text.

---

## Canvas Concepts

### Nodes

Nodes are the visual elements on the canvas.  Each node is a JSON object with
at minimum an `id` and positional data.  Common node types include:

| Node type | Description |
|---|---|
| Text | Free-form text label or note. |
| Wiki page | Links to an existing wiki page (auto-syncs title and slug). |
| Link | External URL reference. |
| Image | Embedded image. |
| Video | Embedded video. |
| Code | Block of source code with an optional language for highlighting. |

The server keeps only these node fields on every write path
(whole-document save, `/ops`, import and history restore):

`id`, `type`, `label`, `display_text`, `content`, `language`, `x`, `y`,
`width`, `height`, `layer`, `text_size`, `color`, `border_color`,
`background_color`, `text_color`, `url`, `image_url`, `embed_url`,
`video_id`, `provider`, `alt`, `page_id`, `page_slug`, `deleted`,
`metadata`, `category`, `rotation`, `opacity`, `shape`, `icon`.

Anything else a client sends is dropped.  In particular the canvas never
stores HTML: a wiki page node's preview comes from
`/api/pages/preview-by-slug` and a code node's highlighting from
`/api/code/highlight`, both rendered by the server for the person viewing
the canvas.  An anonymous visitor of a public canvas gets previews of the
pages public mode lets them read, but highlighting needs a signed-in user,
so they see code nodes as plain text.  Text nodes are escaped before their
simple Markdown is
applied, and a link whose URL uses a scheme other than `http`, `https` or
`mailto` is not made clickable.

### Edges

Edges connect two nodes.  The viewer writes each edge with an `id`, `from`
and `to` node ids, an optional `label` and a `text_size`.  Edges are stored as
JSON objects alongside the nodes and drawn as SVG paths with a text label.

### Viewport

Each layout stores a viewport state (`x`, `y`, and `zoom`) so the view is
restored when a user reopens the canvas.

### Uploaded images

Images added in the canvas editor go through `/api/upload` and live in the
shared upload folder, like images in wiki pages; images restored by an
import end up there too.  The upload cleanup that runs after page edits keeps
every file that a layout, or one of its saved revisions, points at, also
while the canvas plugin is disabled.

---

## Access Control

Canvas access is governed at two levels: global settings and per-layout
permissions.

### Global Settings

Two site settings control who can see and modify canvases across the wiki:

| Setting | Values | Default | Description |
|---|---|---|---|
| `canvas_access` | `admin`, `editor`, `all` | `admin` | Who can view canvas layouts. |
| `canvas_write_access` | `admin`, `editor`, `all` | `admin` | Who can create and import canvas layouts. |
| `canvas_open_access` | on / off | off | Every signed-in user can view and edit every canvas. |
| `canvas_public_access_enabled` | on / off | off | In public mode, anonymous visitors can open canvases marked public (read only). |

These are configured from **Admin → Site Settings**.

### Per-Layout Permissions

Individual layouts can be shared with specific users or roles.  Each permission
entry grants `view`, `edit`, or `none` access:

- **User permissions** override role permissions.
- **Role permissions** apply to all users with that role.
- A `none` permission explicitly denies access.

**Resolution order** for one layout:

1. Admins always have full access.
2. The layout creator always has full access.
3. With `canvas_open_access` on, every signed-in user can edit.
4. User-specific permission (if set).
5. Role-based permission (if set).
6. A layout with `public` visibility can be viewed.
7. Otherwise no access.

The global `canvas_access` setting decides who can open the canvas section at
all.  Users individually shared on a layout bypass that check.  Anyone with
edit access to a layout (for example a collaborator it was shared with for
editing) can change every node on it, so share for editing only with people
you would let rewrite the canvas.

---

## Creating Layouts

From `/canvas`, click **Create Layout** (requires write access).

| Field | Constraint | Description |
|---|---|---|
| Title | Max 200 characters, required | Display name of the layout. |
| Description | Max 2 000 characters, optional | Brief description shown in the layout list. |
| Slug | Auto-generated from title | URL-safe identifier (e.g. `/canvas/my-layout`). |

Layouts are created with default empty canvas data:

```json
{
  "nodes": [],
  "edges": [],
  "viewport": { "x": 0, "y": 0, "zoom": 1 }
}
```

---

## Canvas Data

Layout data is stored as a JSON TEXT column with the following structure:

```json
{
  "nodes": [ { "id": "n1", "label": "Hello", "x": 100, "y": 200, ... } ],
  "edges": [ { "id": "e1", "source": "n1", "target": "n2", ... } ],
  "viewport": { "x": 0, "y": 0, "zoom": 1 }
}
```

**Validation rules:**

- Must be a JSON object with `"nodes"` and `"edges"` keys.
- Serialised size must not exceed 5 MB.
- Nodes keep only the fields listed under [Nodes](#nodes); edges must be
  JSON objects; the viewport is reduced to numeric `x`, `y` and `zoom`.
  Other top-level keys are dropped.
- The data returned by `GET /canvas/<slug>/data` and by the export goes
  through the same filter, so a layout saved by an older version does not
  hand stored HTML to viewers.

Data is saved via `POST /canvas/<slug>/data` and retrieved via
`GET /canvas/<slug>/data`.

---

## Version Tracking

Each layout has a `version` integer (starting at 1) that increments on every
data save.  The version is returned in the JSON API response so clients can
detect concurrent edits.  The `updated_at` timestamp is also updated on each
save.

---

## Wiki Page Integration

Canvas nodes can reference wiki pages by slug.  When a wiki page is updated or
deleted, the canvas system keeps nodes in sync:

- **Page title/slug change:** `canvas_update_wiki_nodes_for_page()` updates
  all canvas nodes that reference the old slug.
- **Page deletion:** `canvas_mark_deleted_wiki_nodes()` marks the affected
  nodes as deindexed so they display a visual indicator.

The integration is one-way. Wiki pages do not reference or display canvas
data.

---

## Export

Users with edit access can export a layout:

```
GET /canvas/<slug>/export
```

The exported JSON contains:

```json
{
  "title": "My Layout",
  "description": "...",
  "version": 5,
  "data": { "nodes": [...], "edges": [...], "viewport": {...} }
}
```

When nodes point at images uploaded to this wiki (`/static/uploads/...`),
the export is a `.canvas.zip` holding `canvas.json` plus those files under
`assets/`.  Add `?format=json` to get the plain `.canvas.json` anyway.

---

## Import

Users with global write access (`canvas_write_access`) can import a
`.canvas.json` or `.canvas.zip` file from the canvas list.  The import
creates a new layout owned by the importing user.

- `canvas.json` may be up to 20 MB (exports are pretty-printed); the parsed
  data must still fit the 5 MB limit above, and its nodes are filtered the
  same way as a save.
- From `assets/`, only files that a node of the imported canvas points at
  are restored; anything else in the zip is ignored.
- Each restored file must have an image extension (`png`, `jpg`, `jpeg`,
  `gif`, `webp`) and pass the same Pillow check as `/api/upload`.  It is
  saved under a new UUID name and the node URLs are rewritten to match, so
  an import can never choose or overwrite a file name in the upload folder.
- All restored images together may not exceed the maximum upload size set
  in **Admin → Settings** (100 MB by default), and each one counts against
  the per-user daily upload quota.  On managed hosting the import is also
  refused when it would pass the wiki's storage limit.
- Files are streamed to disk under temporary names and moved into place
  only when every check has passed, so a refused import leaves nothing
  behind.

---

## Sharing

Admins and layout creators can share a layout with specific users or roles from
the layout's share settings.

| Action | Endpoint |
|---|---|
| Share with user | `POST /canvas/<slug>/share?action=add_user` |
| Share with role | `POST /canvas/<slug>/share?action=add_role` |
| Revoke user | `POST /canvas/<slug>/share?action=remove_user` |
| Revoke role | `POST /canvas/<slug>/share?action=remove_role` |

Permission levels: `view`, `edit`, or `none`.

`UNIQUE` constraints on `(layout_id, user_id)` and `(layout_id, role)` ensure
each user and role has at most one permission entry per layout.

---

## Routes

The routes are under the `/canvas` and `/api/canvas` prefixes.

| Method | Path | Description |
|---|---|---|
| GET | `/canvas` | List layouts visible to the current user |
| POST | `/canvas/create` | Create a new layout (requires write access) |
| POST | `/canvas/import` | Import a `.canvas.json` or `.canvas.zip` file (requires write access) |
| GET | `/canvas/<slug>` | View a canvas layout |
| GET | `/canvas/<slug>/data` | Get layout JSON data (rate-limited: 60 req / 60 s) |
| POST | `/canvas/<slug>/data` | Save layout JSON data (rate-limited: 60 req / 60 s) |
| POST | `/canvas/<slug>/ops` | Apply incremental node and edge operations |
| GET | `/canvas/<slug>/sync` | Fetch operations made by other sessions |
| GET | `/canvas/<slug>/export` | Export layout as `.canvas.json` or `.canvas.zip` |
| POST | `/canvas/<slug>/edit` | Update layout metadata (title, description) |
| POST | `/canvas/<slug>/delete` | Delete a layout and its permissions |
| POST | `/canvas/<slug>/share` | Manage per-layout permissions |
| GET | `/canvas/<slug>/history` | List saved revisions |
| GET | `/canvas/<slug>/history/<entry_id>` | Show one saved revision |
| POST | `/canvas/<slug>/revert/<entry_id>` | Restore a revision (requires edit access) |
| POST | `/canvas/<slug>/history/<entry_id>/delete` | Delete one saved revision (admins only) |
| POST | `/canvas/<slug>/history/clear` | Delete every saved revision of the layout (admins only) |
| POST | `/api/canvas/layout-order` | Save the order of the canvas list |
| GET | `/api/canvas/list-order-version` | Counter the list page polls to notice a new order |

Every route above goes through the same canvas access check as `/canvas`:
it needs the plugin enabled and canvas access (or, for anonymous visitors,
public mode with `canvas_public_access_enabled` and a read-only request).

---

## Database Tables

### `canvas__layouts`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `slug` | TEXT UNIQUE | URL-safe identifier |
| `title` | TEXT NOT NULL | Display title |
| `description` | TEXT | Optional description |
| `category_id` | INTEGER FK | References `categories.id` (optional grouping) |
| `creator_id` | TEXT FK | References `users.id` |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |
| `is_published` | INTEGER | 0 or 1 (default 1) |
| `is_archived` | INTEGER | 0 or 1 (default 0) |
| `data` | TEXT | JSON canvas data (default empty canvas) |
| `version` | INTEGER | Data version counter (default 1) |

### `canvas__permissions`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `layout_id` | INTEGER FK | References `canvas__layouts.id` |
| `user_id` | TEXT FK | References `users.id` (NULL if role-based) |
| `role` | TEXT | Role name (NULL if user-based) |
| `permission` | TEXT CHECK | `view`, `edit`, or `none` (default `view`) |
| `created_at` | TEXT | UTC ISO timestamp |

**Unique constraints:** `(layout_id, user_id)` and `(layout_id, role)`.

---

## Configuration

Canvas global access is controlled by two `site_settings` columns:

| Column | Type | Default | Values |
|---|---|---|---|
| `canvas_access` | TEXT | `admin` | `admin`, `editor`, `all` |
| `canvas_write_access` | TEXT | `admin` | `admin`, `editor`, `all` |

These are configurable from **Admin → Site Settings** without a restart.
