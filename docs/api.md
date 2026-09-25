# JSON API Reference

BananaWiki exposes JSON API endpoints for search, Markdown preview, draft management, accessibility preferences, page reordering, page reservations, canvas data, Kanban boards, image uploads, and the unified API Service plugin.

---

## Authentication

### Session-based (most endpoints)

Most legacy `/api/` endpoints require an active Flask session cookie. Log in first via `POST /login` with `username` and `password` form fields. The server sets a session cookie that must be sent with every subsequent request.

### CSRF protection

Mutating requests authenticated through a browser session must include a CSRF token in the `X-CSRFToken` header or their form data. Obtain the token from the hidden `csrf_token` field rendered on any BananaWiki page:

```
X-CSRFToken: <token>
```

Only the API Service views protected by the Bearer-token authentication decorator are exempt from browser CSRF tokens. Token-management and administrator forms still require CSRF protection.

### Bearer token (API Service plugin)

The API Service endpoints use `Authorization: Bearer <token>` and do not require a session cookie or CSRF token. Enable the `api_service` plugin and manage tokens at `/settings/api-tokens`.

Every token is limited by its selected scopes and read/write permissions, including tokens belonging to administrators. Account permissions are checked separately; a token cannot grant its owner an administrator role. Browser requests that send an `Origin` header must match the server's scheme, hostname, and effective port. Command-line clients may omit that header.

Only administrator accounts (role `admin` or `owner`) can hold the `admin`, `settings` and `users` scopes. The token form drops them for everyone else, and `/api/v1/tokens` refuses them with `403`. Every token created through the form can read; ticking "read only" takes away write access. An expiry entered in the form is read in the site time zone and stored in UTC.

Creating a token through `/api/v1/tokens` requires the `tokens` scope and write access. The new token may have only a subset of the issuing token's permissions. It inherits the issuing token's expiry unless an earlier expiry is supplied. Tokens are separate credentials: revoking an issuing token does not automatically revoke tokens it previously created. Review and revoke those credentials separately in token management.

A token follows the state of the account that owns it:

- A token of a suspended account is rejected with `401`.
- While the account has to change its password, or an administrator still has to finish onboarding, its tokens are rejected with `403`. The web interface holds the account on the same step.
- In maintenance mode, tokens of accounts that are not administrators are rejected with `503`. Administrators keep working so they can finish the maintenance.
- Changing an account's password through `PUT /api/v1/users/<id>` revokes every API token of that account, the userbot token included, and ends its web sessions. When administrators change their own password this way, the token that made the call is revoked too. Any other code that changes or resets a password should call `db.revoke_user_api_service_tokens(user_id, reason)` to do the same; it returns the number of tokens revoked.

API audit records retain the actor, endpoint, response status, address, and timing, without recording request bodies that may contain passwords or private content. See [migration notes](../MIGRATION.md) for existing deployments.

---

## Error responses

All endpoints return errors in a consistent JSON format:

```json
{
  "error": "Description of the error"
}
```

Common HTTP status codes:

| Status | Meaning |
|--------|---------|
| `400`  | Bad request: missing or invalid parameters |
| `401`  | Not authenticated |
| `403`  | Forbidden: insufficient permissions |
| `404`  | Resource not found |
| `409`  | Conflict, such as a slug that already exists or a page another editor holds |
| `413`  | Request body too large |
| `429`  | Rate limit exceeded |
| `500`  | Internal server error |
| `503`  | API Service disabled, banana mode on, or maintenance mode for a non-administrator token |

---

## Rate limiting

All mutation endpoints are rate-limited. When a limit is exceeded the server returns `429 Too Many Requests` with:

```json
{
  "error": "Rate limit exceeded. Please try again later."
}
```

Rate limits are tracked per IP address in the database and are shared across all Gunicorn workers. Specific limits are noted per endpoint below.

---

## Search

### Search wiki pages

```
GET /api/pages/search?q=<query>&limit=<n>
```

| Parameter | Type   | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `q`       | string | yes      | - | Search query |
| `limit`   | int    | no       | 20      | Max results  |

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "results": [
    {
      "id": 42,
      "title": "Getting Started",
      "slug": "getting-started",
      "snippet": "...matched text..."
    }
  ]
}
```

### Search sidebar categories

```
GET /api/sidebar/search?q=<query>
```

| Parameter | Type   | Required | Description |
|-----------|--------|----------|-------------|
| `q`       | string | yes      | Search query |

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "results": [
    {
      "id": 5,
      "name": "Tutorials",
      "slug": "tutorials"
    }
  ]
}
```

---

## Markdown preview

```
POST /api/preview
Content-Type: application/json
```

**Body:**
```json
{
  "content": "# Hello\n\nSome **Markdown** text."
}
```

**Rate limit:** 30 requests / 60 seconds

**Response** `200`:
```json
{
  "html": "<h1>Hello</h1>\n<p>Some <strong>Markdown</strong> text.</p>"
}
```

The rendered HTML is sanitised through Bleach using the same `ALLOWED_TAGS` and `ALLOWED_ATTRS` as wiki page rendering.

---

## Draft management

Drafts auto-save editor content so work is not lost. Requires the **drafts** plugin to be enabled.

### Save draft

```
POST /api/draft/save
Content-Type: application/json
```

**Body:**
```json
{
  "page_id": 42,
  "content": "Draft content..."
}
```

**Rate limit:** 30 requests / 60 seconds

**Response** `200`:
```json
{
  "status": "ok"
}
```

### Load draft

```
GET /api/draft/load/<page_id>
```

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "draft": {
    "content": "Draft content...",
    "updated_at": "2026-04-11T12:00:00"
  }
}
```

Returns `404` if no draft exists for the given page.

### List other users' drafts

```
GET /api/draft/others/<page_id>
```

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "drafts": [
    {
      "user_id": "abc12345",
      "username": "alice",
      "updated_at": "2026-04-11T10:30:00"
    }
  ]
}
```

### Transfer draft

Transfer another user's draft to the current user (admin/editor action).

```
POST /api/draft/transfer
Content-Type: application/json
```

**Body:**
```json
{
  "page_id": 42,
  "from_user_id": "abc12345"
}
```

**Rate limit:** 30 requests / 60 seconds

### Delete draft

```
POST /api/draft/delete
Content-Type: application/json
```

**Body:**
```json
{
  "page_id": 42
}
```

**Rate limit:** 30 requests / 60 seconds

### List all user's drafts

```
GET /api/draft/mine
```

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "drafts": [
    {
      "page_id": 42,
      "page_title": "Getting Started",
      "content": "Draft content...",
      "updated_at": "2026-04-11T12:00:00"
    }
  ]
}
```

---

## Accessibility preferences

### Update preferences

```
POST /api/accessibility
Content-Type: application/json
```

**Body:** A JSON object of accessibility preference key-value pairs:

```json
{
  "high_contrast": true,
  "large_text": false,
  "reduce_motion": true
}
```

**Rate limit:** 60 requests / 60 seconds

**Response** `200`:
```json
{
  "status": "ok"
}
```

### Reset preferences

```
POST /api/accessibility/reset
```

**Rate limit:** 10 requests / 60 seconds

Resets all accessibility preferences to defaults.

---

## Reordering

### Reorder pages in a category

```
POST /api/reorder/pages
Content-Type: application/json
```

**Body:**
```json
{
  "order": [3, 1, 7, 2]
}
```

The `order` array contains page IDs in the desired display order.

**Rate limit:** 60 requests / 60 seconds\
**Requires:** editor role or higher

### Reorder categories

```
POST /api/reorder/categories
Content-Type: application/json
```

**Body:**
```json
{
  "order": [5, 2, 8, 1]
}
```

**Rate limit:** 60 requests / 60 seconds\
**Requires:** editor role or higher

---

## Page reservations

The page reservation (checkout) system allows editors to claim exclusive editing rights on a page. Requires the **page_reservations** plugin.

### Check reservation status

```
GET /api/pages/<page_id>/reservation/status
```

**Response** `200`:
```json
{
  "reserved": true,
  "reserved_by": "abc12345",
  "reserved_by_username": "alice",
  "reserved_at": "2026-04-10T08:00:00",
  "expires_at": "2026-04-12T08:00:00"
}
```

If the page is not reserved:
```json
{
  "reserved": false
}
```

### Reserve a page

```
POST /api/pages/<page_id>/reservation
```

**Rate limit:** 30 requests / 60 seconds\
**Requires:** editor role or higher

**Response** `200`:
```json
{
  "status": "ok"
}
```

Returns `409` if the page is already reserved by another user, or `403` if the user is in a cooldown period.

### Release a reservation

```
DELETE /api/pages/<page_id>/reservation
```

**Rate limit:** 30 requests / 60 seconds

Releases the current user's reservation on the page. Admins can release any reservation.

---

## Canvas API

Canvases are visual node-and-edge layouts for organising information. Access is controlled by `canvas_access` and `canvas_write_access` site settings.

### Get canvas data

```
GET /canvas/<slug>/data
```

**Response** `200`:
```json
{
  "data": {
    "nodes": [],
    "edges": [],
    "viewport": { "x": 0, "y": 0, "zoom": 1 }
  },
  "version": 3
}
```

### Save canvas data

```
POST /canvas/<slug>/data
Content-Type: application/json
```

**Body:**
```json
{
  "data": {
    "nodes": [
      { "id": "n1", "type": "text", "x": 100, "y": 200, "width": 300, "height": 150, "content": "Hello" }
    ],
    "edges": [
      { "id": "e1", "source": "n1", "target": "n2" }
    ],
    "viewport": { "x": 0, "y": 0, "zoom": 1 }
  }
}
```

**Size limit:** 5 MB maximum payload\
**Requires:** write access to the canvas

### Export canvas

```
GET /canvas/<slug>/export
```

Downloads the canvas as a `.json` file attachment.

---

## Kanban API

Kanban endpoints manage boards, columns, tickets, attachments, history, and comments. Access is controlled by `kanban_access` and `kanban_write_access` site settings plus per-board sharing.

### Columns

#### Create column

```
POST /api/kanban/<board_id>/columns
Content-Type: application/json
```

**Body:**
```json
{
  "title": "In Progress"
}
```

#### Edit column

```
PUT /api/kanban/columns/<column_id>
Content-Type: application/json
```

**Body:**
```json
{
  "title": "Done"
}
```

#### Delete column

```
DELETE /api/kanban/columns/<column_id>
```

#### Reorder columns

```
POST /api/kanban/<board_id>/columns/reorder
Content-Type: application/json
```

**Body:**
```json
{
  "order": [3, 1, 2]
}
```

### Tickets

#### Create ticket

```
POST /api/kanban/columns/<column_id>/tickets
Content-Type: application/json
```

**Body:**
```json
{
  "title": "Fix login bug",
  "description": "Optional markdown description"
}
```

#### Get ticket

```
GET /api/kanban/tickets/<ticket_id>
```

**Response** `200`:
```json
{
  "id": 7,
  "title": "Fix login bug",
  "description": "...",
  "column_id": 2,
  "created_by": "abc12345",
  "created_at": "2026-04-11T12:00:00"
}
```

#### Edit ticket

```
PUT /api/kanban/tickets/<ticket_id>
Content-Type: application/json
```

**Body:**
```json
{
  "title": "Updated title",
  "description": "Updated description"
}
```

#### Delete ticket

```
DELETE /api/kanban/tickets/<ticket_id>
```

#### Move ticket

```
POST /api/kanban/tickets/<ticket_id>/move
Content-Type: application/json
```

**Body:**
```json
{
  "column_id": 3,
  "position": 0
}
```

#### Reorder tickets

```
POST /api/kanban/columns/<column_id>/tickets/reorder
Content-Type: application/json
```

**Body:**
```json
{
  "order": [5, 2, 8]
}
```

### Ticket attachments

#### Upload attachment

```
POST /api/kanban/tickets/<ticket_id>/attachments
Content-Type: multipart/form-data
```

**Form field:** `file`, the file to upload (max 5 MB)

**Rate limit:** 20 requests / 60 seconds

#### List attachments

```
GET /api/kanban/tickets/<ticket_id>/attachments
```

#### Delete attachment

```
DELETE /api/kanban/attachments/<attachment_id>
```

**Rate limit:** 20 requests / 60 seconds

#### Download attachment

```
GET /api/kanban/attachments/<attachment_id>/download
```

### Ticket history

#### List history entries

```
GET /api/kanban/tickets/<ticket_id>/history
```

#### Get history entry

```
GET /api/kanban/history/<entry_id>
```

### Ticket comments

#### List comments

```
GET /api/kanban/tickets/<ticket_id>/comments
```

#### Add comment

```
POST /api/kanban/tickets/<ticket_id>/comments
Content-Type: application/json
```

**Body:**
```json
{
  "content": "Looks good!"
}
```

**Rate limit:** 30 requests / 60 seconds

#### Edit comment

```
PUT /api/kanban/comments/<comment_id>
Content-Type: application/json
```

**Body:**
```json
{
  "content": "Updated comment"
}
```

**Rate limit:** 30 requests / 60 seconds

#### Delete comment

```
DELETE /api/kanban/comments/<comment_id>
```

**Rate limit:** 30 requests / 60 seconds

### Board settings

#### Get settings

```
GET /api/kanban/<board_id>/settings
```

#### Update settings

```
PUT /api/kanban/<board_id>/settings
Content-Type: application/json
```

---

## Upload API

### Upload image

```
POST /api/upload
Content-Type: multipart/form-data
```

**Form field:** `file`, image file (png, jpg, jpeg, gif, webp; max 16 MB)

**Rate limit:** 10 requests / 60 seconds\
**Requires:** editor role or higher

**Response** `200`:
```json
{
  "url": "/static/uploads/abc123.png"
}
```

### Delete image

```
POST /api/upload/delete
Content-Type: application/json
```

**Body:**
```json
{
  "filename": "abc123.png"
}
```

**Rate limit:** 10 requests / 60 seconds

---

## API Service Plugin (Bearer token)

The API Service plugin uses Bearer-token authentication and is CSRF-exempt. It is pre-installed but disabled by default. See [Banana Mode API documentation](banana_mode_api.md) and the in-app `/api-docs` page for full details.

| Method | Endpoint | Scope | Who |
|--------|----------|-------|-----|
| `GET`  | `/api/v1/status` | none | anyone |
| `GET`, `POST` | `/api/v1/users` | `users` | administrators |
| `GET`, `PUT`, `DELETE` | `/api/v1/users/<id>` | `users` | administrators |
| `POST` | `/api/v1/users/bulk` | `users` | administrators |
| `GET`, `POST` | `/api/v1/pages` | `pages` | readers; writing needs an editor |
| `GET`, `PUT`, `DELETE` | `/api/v1/pages/<slug>` | `pages` | readers; writing needs an editor, deleting also `page.delete` |
| `POST` | `/api/v1/pages/bulk`, `/bulk-edit`, `/bulk-delete` | `pages` | administrators |
| `GET`, `POST` | `/api/v1/categories` | `categories` | readers; creating needs `category.create` |
| `PUT`, `DELETE` | `/api/v1/categories/<id>` | `categories` | `category.edit` / `category.reorder`; deleting needs an administrator |
| `GET`, `PUT` | `/api/v1/settings` | `settings` | administrators |
| `GET`, `POST` | `/api/v1/tokens`, `DELETE /api/v1/tokens/<id>` | `tokens` | the token's owner |
| `GET`  | `/api/v1/admin/tokens`, `/api/v1/admin/audit-log` | `admin` | administrators |
| `POST` | `/api/v1/admin/tokens/<id>/revoke` | `admin` | administrators |
| `PUT`  | `/api/v1/admin/users/<id>/api-access` | `admin` | administrators |
| `DELETE` | `/api/v1/admin/audit-log` | `admin` | superusers |
| `GET`, `POST` | `/api/v1/banana-mode` | `admin` | administrators |
| `GET`  | `/api/v1/userbot/me` | `userbot` | the token's owner |
| `POST` | `/api/v1/userbot/profile` | `userbot` | the token's owner |

**Authentication:**
```
Authorization: Bearer <token>
```

**Rate limit:** configured in Admin -> API Service, between 1 and 10,000 requests per minute for each account, with a separate limit for administrators. Each account can hold between 1 and 100 tokens, as configured there.

**Request size:** a JSON body can be up to 16 MB, with or without a `Content-Length` header, or less when the server's own request size limit is lower. Larger bodies get `413`.

### Users

User management applies the rules of the account pages:

- Usernames are 3 to 50 characters of letters, digits, underscores and hyphens.
- Passwords are 8 to 1024 characters. Surrounding whitespace is removed.
- Superuser accounts cannot be changed or deleted through the API, and the owner account only by the owner. The API cannot make anyone the owner.
- `POST /api/v1/users/bulk` creates up to 20 accounts per request. Each account costs a password hash, so larger imports need several requests. Items that fail (a taken username, a short password) are listed in `errors` and the others are created.

### Pages

Page writes apply the editor's limits: content up to 1,000,000 characters, a title of 1 to 200 characters, and a slug normalised as the editor makes it (`"My Page"` becomes `my-page`) that is at most 200 characters long. Category ids must be whole numbers; anything else gets `400`. Creating, editing and deleting a page tell plugins about it, as the editor does, so for example cached text-to-speech audio is refreshed or removed.

`DELETE /api/v1/pages/<slug>` needs the `page.delete` permission, which editors do not have by default. It refuses the home page, pages protected or checked out by someone else, pages scheduled for automatic deletion and pages already pending deletion. With the Deletion Slowdown plugin on, the page waits out its grace period (`202`), unless it is in the documentation category and documentation is set to bypass the delay.

The bulk endpoints take up to 100 items. Every item is checked for shape and limits before anything is written, so one malformed item refuses the whole request with `400`. Problems found while applying an item, such as an existing slug or a missing category, are reported per item. `bulk-delete` applies the single-delete rules to every page and counts refused pages as `skipped`.

`GET /api/v1/categories` lists only the categories the caller may read.

### Settings

`GET /api/v1/settings` returns the site settings without secrets. `PUT /api/v1/settings` accepts the settings of the admin settings page with the same checks: colours, time zones, languages and choices must be valid, and numbers are clamped to the form's range. Settings that belong to a plugin need that plugin to be enabled, which also means EasyWiki instances cannot change the settings of the plugins EasyWiki turns off. Features the hosting platform has switched off (public access, the page builder, text-to-speech generation) cannot be switched on, and on hosted instances the upload size belongs to the platform.

The API refuses settings the wiki or the hosting platform maintain (`setup_done`, `last_server_restart_at`, `last_chat_cleanup_at`, `list_order_version`, `chat_cleanup_split_configured`, `docs_category_id`, `platform_upload_blacklist`, `devtools_enabled`), the remote GPU settings (`tts_gpu_*`), settings managed on their own page (custom favicons and interface languages), the legacy chat cleanup settings and anything secret.

The whole request is checked first. Invalid values get `400` with an `invalid` object, and settings that cannot change get `403` with a `refused` object; in both cases nothing is saved. A value equal to the stored one is accepted for any setting except secrets, so a client can send back what `GET` returned with its own changes applied. A successful response lists the keys that changed in `updated`.
