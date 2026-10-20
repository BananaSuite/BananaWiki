# REST API

BananaWiki has a JSON API under `/api/v1` for scripts and integrations. It
does exactly what the token's owner could do in the browser, never more:
every call applies the owner's role, permissions and category access through
the same code the web interface uses.

The machine-readable description is served at `/api/v1/openapi.json`
(OpenAPI 3.1) and a readable overview at `/api-docs`. `GET /api/v1/status`
needs no token and answers `{"ok": true, "api_enabled": …, "version": …}`.

This page is about the token API. The web interface's own JSON endpoints
under `/api/` (previews, drafts, sidebar, kanban and canvas updates …) use the
browser session and the CSRF token; they are not a stable interface for
scripts.

## Switching it on

The API is the **REST API** feature (plugin id `api_service`, off by
default). An administrator enables it under **Admin → Plugins**, then opens
**Admin → REST API** (`/admin/api-service`) and ticks *Enable the REST API*
(`site_settings.api_service_enabled`). While the feature is off every
`/api/v1` URL answers 404; while the switch is off they answer 503.

The same page sets the per-minute request limits for members
(`api_service_rate_limit`, default 60) and administrators
(`api_service_admin_rate_limit`, default 120), and the number of tokens an
account may hold (`api_service_max_tokens_per_user`, default 5). Values are
clamped to 1–10000 and 1–100.

Administrators and owners may always use the API. Other accounts need API
access switched on for them (`users.api_access_enabled`), on the same page or
with `PUT /api/v1/admin/users/<id>/api-access`.

## Tokens

Each account creates its tokens on **Settings → API tokens**
(`/settings/api-tokens`); an administrator viewing the wiki as someone else
(impersonation) cannot create tokens or turn on automation mode for them.
The token is shown once; only an HMAC-SHA256 of it
is stored (`api_service__tokens.token_hash`, keyed by the instance secret key
with the 1.4 label, so tokens issued by 1.4 keep working). Moving the wiki to
a different secret key invalidates every token.

A token carries its own grant:

```json
{"read": true, "write": false, "scopes": ["pages", "categories"]}
```

| Scope | Endpoints |
|---|---|
| `pages` | pages, search, history, page attachments |
| `categories` | categories |
| `kanban` | kanban boards, columns, tickets, comments, ticket attachments |
| `canvas` | canvases |
| `tokens` | the caller's own tokens |
| `users` | accounts (administrators only) |
| `settings` | site settings (administrators only) |
| `admin` | all tokens, API access, audit log, webhooks (administrators only) |
| `userbot` | `/userbot/*` (the automation key only; see below) |

`GET` endpoints need `read`, everything else needs `write`. The `users`,
`settings` and `admin` scopes are only given to administrators; the token
form drops them for other accounts, and an old token of a demoted account
gets `403` from those endpoints. A token can have an expiry.

Send the token in the `Authorization` header:

```sh
curl -H "Authorization: Bearer $TOKEN" https://wiki.example.org/api/v1/pages
```

Bearer calls need no cookie and no CSRF token. A request carrying an
`Origin` header from another site is refused, so a web page cannot drive the
API with a token it obtained.

Tokens stop working when they are revoked, expire, or when their account:

* is suspended, pending approval or denied (`403 account_blocked`);
* must change its password or finish onboarding (`403`);
* is not an administrator while the wiki is in maintenance mode (`503`);
* changes its password anywhere, has it reset by an administrator, or is
  suspended by an administrator (all tokens are revoked, including the
  userbot key);
* is deleted (tokens are deleted with it).

## Responses and errors

Successful answers have `"ok": true`. Errors always look like this:

```json
{"ok": false, "error": "Page not found.", "code": "page_not_found"}
```

`code` is stable and meant for programs; application errors are translated
into the caller's language. Unexpected server/storage failures and HTTP routing
errors use a generic message and include a `request_id`, also sent in the
`X-Request-ID` header. Some errors add fields: `field` (invalid input),
`scope`/`write` (`scope_missing`), `revision` (`edit_conflict`), `invalid`
and `refused` (settings).

| Status | Meaning |
|---|---|
| 400 | invalid input (`invalid_input`, `category_missing`, …) |
| 401 | missing, invalid, revoked or expired token |
| 403 | the token or its owner may not do this |
| 404 | not found, or not readable by the caller |
| 409 | conflict: slug taken, page protected or checked out, edit conflict, already pending deletion, `Idempotency-Key` request running or not replayable |
| 412 | `If-Match` names an older version (`precondition_failed`, with `revision` or `version`) |
| 413 | body larger than 2 MiB (or an upload over its limit) |
| 422 | `Idempotency-Key` reused for a different request or by another token |
| 429 | rate limit (per account, per minute; `Retry-After` in seconds) |
| 500 | unexpected server error (`internal_error`) |
| 503 | API switched off, maintenance mode, or temporary storage failure (`storage_unavailable`) |

Endpoints of a feature that is switched off (Kanban, Canvas, Attachments)
answer 404.

Timestamps are ISO 8601 in UTC (`2025-01-31T09:30:00Z`). Request bodies must
be JSON objects with valid Unicode, finite numbers and at most 64 nested
objects/arrays. Booleans are JSON `true`/`false` (`0`/`1` are accepted);
strings like `"false"` are refused.

## Conventions

* **Paging.** Every list takes `limit` and `offset` and answers with
  `limit`, `offset` and `next_offset` (null on the last page).
* **Rate limit headers.** Every authenticated answer carries
  `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`
  (seconds until a request is freed).
* **Optimistic concurrency.** `GET/POST/PUT /pages/<slug>` answer with
  `ETag: "r<revision>"`, canvas answers with `ETag: "v<version>"`. Send it
  back as `If-Match` on `PUT /pages/<slug>` or `PUT /canvas/<slug>/document`
  and the change is refused with 412 when someone saved in between
  (`If-Match: *` accepts any version).

## Retrying safely (`Idempotency-Key`)

A `POST` sent with `Idempotency-Key: <1–255 visible ASCII characters>` runs
once. Retrying it with the same key, method, path and body within 24 hours
returns the stored answer with `Idempotent-Replayed: true` instead of running
it again. A key is unique per account and bound to the token that first used
it: the same key with a different request, or from another token, is refused
with 422 `idempotency_key_reused`; a retry while the first request is still
running gets 409 `idempotency_in_progress`. Server errors and 429 answers are
not stored, so those requests can simply be retried. File uploads
(multipart) refuse the header.

The database changes and replay record commit together; a server error or
interrupted worker rolls back the keyed write. Attachment files removed by
bulk deletion are retained until that commit. Responses larger than 2 MiB
retain only their status; retries return `idempotency_replay_unavailable`
with that status and do not repeat the operation.

Replays apply the token owner's current role and access to returned pages,
categories, canvases and boards. A resource that became private, unreadable or
deleted returns 409 `idempotency_replay_unavailable` without its old contents
or a second write. Successful page and canvas replays retain their `ETag`.

Answers that contain a new secret are never stored: a new token
(`POST /tokens`) and a webhook secret (`POST /admin/webhooks`,
`POST /admin/webhooks/<id>/rotate-secret`). A retry of such a request that
succeeded is refused with 409 `idempotency_replay_unavailable` (with the
first answer's `status`) and is **not** run again; list your tokens or
webhooks to find what was created, revoke or rotate it if the secret was
lost, and use a new key for a new request. Refusals (4xx) of these requests
are stored and replayed as usual.

## Pages (`pages`)

| | |
|---|---|
| `GET /pages?category_id=&limit=&offset=` | pages the caller can read (`category_id=none` for uncategorised); at most 500 per request |
| `GET /pages/<slug>` | one page with its Markdown and `revision` |
| `POST /pages` | `{title, content?, slug?, category_id?, edit_message?}` → 201 |
| `PUT /pages/<slug>` | `{title?, content?, category_id?, expected_revision?, edit_message?}` |
| `DELETE /pages/<slug>` | 200 `{"deleted": true}` or 202 `{"pending_deletion": true}` |
| `GET /pages/<slug>/history` | revisions (needs `history.view`) |
| `GET /history/<id>` | one revision with its content |
| `GET /search?q=&titles_only=&limit=&offset=` | full-text search (needs `search.pages`) |
| `POST /pages/bulk`, `/pages/bulk-edit`, `/pages/bulk-delete` | up to 100 items, administrators only |

The rules are the web editor's:

* A page the caller cannot read answers 404 and is left out of lists and
  search.
* Creating needs `page.create` and write access to the category; editing
  needs `page.edit_all` and write access; changing the title or moving the
  page also needs `page.edit_metadata`, and a move needs write access to both
  categories. Deleting needs `page.delete`.
* A protected page or one checked out by someone else answers 409
  `page_locked`.
* With deletion slowdown on, `DELETE` answers 202 and starts the grace
  period.
* A new page's slug comes from `slug` or the title; an existing slug is a
  409, so a client can create and update on 409.
* `expected_revision` turns a stale update into 409 `edit_conflict`.
* Creating in a category that does not exist answers 400.

Bulk operations validate every item before writing anything, then report
per-item problems in `errors`.

### Page attachments (`pages`, Attachments feature)

`GET /pages/<slug>/attachments`, `GET /pages/<slug>/attachments/<id>` (the
file), `POST /pages/<slug>/attachments` (`multipart/form-data`, field
`file`), `DELETE /pages/<slug>/attachments/<id>`. The attachment panel's
rules apply: `attachment.view` to list and download, edit rights on an
unprotected page plus `attachment.upload` to upload (counting towards the
daily upload quota), `attachment.delete_any` or `attachment.delete_own` to
delete.

## Categories (`categories`)

`GET /categories`, `GET /categories/<id>` (with its readable pages),
`POST /categories {name, parent_id?}`,
`PUT /categories/<id> {name?, parent_id?, sequential_nav?}`,
`DELETE /categories/<id>?page_action=uncategorize|move|delete&target_id=`.

Creating needs `category.create`, renaming `category.edit`, moving
`category.reorder` (with write access to the category and the new parent),
sequential navigation `category.manage_sequential`, deleting
`category.delete`. A category cannot move into itself or its subcategories;
names are at most 100 characters. `GET /categories` lists categories only
for callers with `category.view_all`.

`page_action=delete` also needs `page.delete` and a token with the `pages`
scope and write access (otherwise 403 `cannot_delete` or `scope_missing`).
It deletes the pages as `DELETE /pages/<slug>` would, all or nothing: if the
category holds pages the caller cannot see or may not delete, the answer is
403 (`category_pages_hidden`, `category_pages_forbidden`); protected,
checked-out or pending pages give 409 (`category_pages_blocked`). Refused
pages are listed by slug in `pages`. With deletion slowdown on, the answer is
202 and `pending_deletion` lists the scheduled pages; they, and pages another
feature keeps (`kept`), lose the category.

## Kanban (`kanban`)

| | |
|---|---|
| `GET /kanban/boards` | boards the caller can open, in their order; `?archived=1` lists the archived ones instead |
| `POST /kanban/boards` | `{title, description?, default_columns?}` → 201 |
| `GET /kanban/boards/<id>` | the board with `columns` (ticket ids in order) and every active ticket's card in `tickets`; with filter parameters also `filter`, `shown` and `total` |
| `PUT /kanban/boards/<id>` | `{title?, description?, visibility?}` |
| `DELETE /kanban/boards/<id>` | creator and administrators |
| `POST /kanban/boards/<id>/archive` / `restore` | creator and administrators |
| `GET /kanban/boards/<id>/archived-tickets` | archived tickets, most recently archived first (the newest 500), with `column_title`, `archived_at`, `archived_by_username`, and `total` |
| `POST /kanban/boards/<id>/tickets/archive` / `restore` | `{ids: [ticket ids]}` (all on this board; at most 500 ids, repeats included, else 400 `too_many`) → `archived` / `restored` count |
| `POST /kanban/boards/<id>/columns` | `{title}` |
| `POST /kanban/boards/<id>/columns/reorder` | `{order: [column ids]}` |
| `PUT` / `DELETE /kanban/columns/<id>` | `{title?, wip_limit?, done?}` (rename, set the work-in-progress limit, `null` or `0` removes it, and/or mark the column done) / delete with its tickets |
| `POST /kanban/columns/<id>/archive` | archive every active ticket of the column → `archived` count |
| `POST /kanban/columns/<id>/tickets` | `{title, description?, priority?, color?, due_date?, labels?, assignees?}` |
| `GET` / `PUT` / `DELETE /kanban/tickets/<id>` | one ticket with its description / change / delete |
| `POST /kanban/tickets/<id>/move` | `{column_id, position?}` (0-based; omitted: at the end) |
| `POST /kanban/tickets/<id>/archive` / `restore` | → the `ticket` and `changed` (false when it already was) |
| `GET` / `POST /kanban/tickets/<id>/checklist` | the checklist `[{id, text, done}]` / add an item `{text}` → 201 |
| `POST /kanban/tickets/<id>/checklist/reorder` | `{order: [item ids]}` |
| `PUT` / `DELETE /kanban/checklist/<item id>` | `{text?, done?}` / delete |
| `GET /kanban/my-tickets` | tickets assigned to the caller on boards they can open, earliest due date first, with `board_title` and `column_title` (paginated) |
| `GET` / `POST /kanban/tickets/<id>/comments` | list / add `{content}` |
| `PUT` / `DELETE /kanban/comments/<id>` | your own comments (administrators: any) |
| `GET` / `POST /kanban/tickets/<id>/attachments` | list / upload (multipart, field `file`) |
| `GET` / `DELETE /kanban/attachments/<id>` | download / delete |

The board rules are the web interface's: a board the caller cannot see is
404 with everything on it, a board they can see but not change 403; viewers
may comment; visibility and deletion belong to the creator and
administrators; creating boards needs `kanban.create` and global write
access. Ticket titles accept the board shorthand (`@user`, `+label`,
`!high`, `due:tomorrow`, `color:red`) for fields the request does not set.
Assignees must be able to reach the board. Changes appear live on open
boards and in the board's activity and history.

Tickets include `checklist_total` and `checklist_done`; columns include
`wip_limit` (`null` for none; a full column still accepts tickets, the board
only warns) and `done` (`true` for a column of finished work: its tickets are
never overdue or due soon, also in the `due` filters). Checklist changes need
write access to the board and answer the whole `checklist` with the updated
`ticket`; a ticket holds at most 100 items of up to 300 characters.

**Archive.** Archiving tickets needs write access to the board. An archived
ticket keeps its column and fields but is left out of the board, its
filters and "my tickets"; it can still be read, changed, commented on and
deleted, but moving it answers 409 `ticket_archived`. Restoring puts it at
the end of its column. Tickets and boards carry `archived_at` (`null` while
active). Only the board's owner (its creator or an administrator) archives
and restores a board; an archived board is read-only for everyone (writes
and comments answer 403 `board_forbidden`), while the owner can still
change its visibility, delete it, and restore it.

**Filters.** `GET /kanban/boards/<id>` accepts the board page's filter
parameters: `q` (every word in the title, `#id`, `+label` or `@username`),
`who` (`me`, `none` or a user id), `label`, `priority`
(`low`/`medium`/`high`/`critical`) and `due` (`overdue`, `soon`, `week`,
`none`, compared with today in the site's time zone). `GET /kanban/my-tickets`
accepts the same except `who`. Invalid values answer 400 `invalid_filter`.

## Canvas (`canvas`)

| | |
|---|---|
| `GET /canvas` | canvases the caller can open |
| `POST /canvas` | `{title, description?, data?}` (`data`: `{nodes, edges, viewport?}`) → 201 |
| `GET /canvas/<slug>` | `canvas` (with `version`) and its document `data`, `ETag: "v<version>"` |
| `PUT /canvas/<slug>` | `{title?, description?, visibility?}` (visibility: creator and administrators) |
| `PUT /canvas/<slug>/document` | replace the document `{data, expected_version?}`, `If-Match` supported |
| `POST /canvas/<slug>/ops` | apply up to 500 editor operations (`upsert_node`, `delete_node`, `upsert_edge`, …) atomically |
| `GET /canvas/<slug>/history` | saved versions |
| `DELETE /canvas/<slug>` | creator and administrators |

A canvas the caller cannot see is 404, one they may only view 403 for
changes. Wiki-page nodes pointing at pages the caller cannot read come back
without title and slug. Documents are cleaned like the editor's saves; a
stale `expected_version` is 409 `edit_conflict` (with `version`).

Locked elements are protected on the server too: an operation batch or a
document save that would move, resize, edit or delete a locked node is
refused as a whole with 400 `locked`; `locked` lists the ids of the locked
nodes concerned (at most 10) and `locked_count` how many there are. Unlock
them first: an `upsert_node` that only changes `locked` to `false` is
allowed, unlocking and editing in the same operation is not.
A wiki-page node keeps its stored page link while it is locked: a change of
`page_id` or `page_slug` on a locked node is dropped and the rest of the
operation applies, and a wiki-page node sent without `page_id` or `page_slug`
keeps the link it has. To link another page, unlock the node first.
A `PUT /canvas/<slug>` that changes both the title or description and the
visibility sends two `canvas.updated` webhook events.

## Accounts (`users`, administrators)

`GET /users`, `GET /users/<id>`, `POST /users {username, password, role?,
force_password_change?}`, `POST /users/bulk {users: [...]}` (at most 20),
`PUT /users/<id> {role?, suspended?, api_access_enabled?, password?}`,
`DELETE /users/<id>`.

Usernames are 3–50 letters, digits, `_` or `-`; passwords follow the web
password rules. The [hierarchy of administrators](permissions.md#roles)
applies exactly as in the admin pages: superusers and owners are changed only
by themselves, another administrator only by an owner or a superuser (403
`protected_account`); nobody changes their own role (400
`cannot_change_own_role`) or suspends or deletes themselves; the owner role
cannot be given; the last administrator cannot be demoted, suspended or
deleted. Role changes and suspensions go through the same code as the admin
pages: a new role drops a custom role and individual permissions and is
recorded in the role history, `suspended: true` is a permanent suspension
without a reason, both are recorded and sent to webhooks
(`user.role_changed`, `user.suspended`). Setting a password signs the account
out everywhere and revokes its tokens (`api_tokens_revoked` in the answer).
Setting the caller's own password also needs `current_password`, as on the
account page (403 `current_password_required` or `current_password_wrong`),
so a token alone cannot take over its account. Wrong current passwords count
against the account page's limit (10 in 15 minutes, shared with it): then
429 `too_many_attempts`. No call creates an owner or a
superuser.

## Settings (`settings`, administrators)

`GET /settings` returns the site settings without secrets and without
columns of retired features. `PUT /settings` changes only the settings in the
allow-list (`settings_rules.py`), with the bounds and choices of the admin
forms, the feature they belong to switched on, and the hosting platform's
restrictions. Unknown keys are 400; internal, platform-managed, remote-GPU,
retired and secret keys are 403. **Nothing is saved when anything is
refused.** A value equal to the stored one is always accepted, so a client
can send back what `GET` returned with its changes. Expiry times without an
offset are read in the site time zone.

## Tokens (`tokens`) and administration (`admin`)

* `GET /tokens`, `DELETE /tokens/<id>`: the caller's own tokens.
* `POST /tokens {name?, permissions?, expires_at?}`: issue a token that can
  never exceed the calling one: no flag or scope it lacks, no later expiry;
  an omitted expiry inherits the caller's. An expiry is at most 10 years
  ahead (400 `expiry_too_far`), on the token page too. The name `userbot`
  (in any case) is kept for the automation key (400 `token_name_reserved`).
  The new token remembers the token that issued it: revoking or expiring
  that token (or anything that revokes it) revokes the tokens it issued. An
  issued token cannot have the `tokens` scope itself (403
  `tokens_scope_forbidden`) or the `userbot` scope (403
  `userbot_scope_reserved`).
* `GET /admin/tokens`, `POST /admin/tokens/<id>/revoke` (the tokens of a
  superuser, an owner or another administrator only as the hierarchy allows),
  `PUT /admin/users/<id>/api-access {enabled}`,
  `GET /admin/audit-log?limit=&offset=` (the address of an account the caller
  may not manage, as on the sessions page, is `null` with `ip_hidden: true`),
  `DELETE /admin/audit-log?before_days=` (superusers).

## Webhooks (`admin`, administrators)

Webhooks tell other services about wiki events. Configure them on
**Admin → REST API** (the secret is shown once, with a delivery log per
webhook and *Send test* / *Send again* buttons) or through the API:

* `GET /admin/webhooks` (with the list of `events`),
  `POST /admin/webhooks {url, events, description?, active?, allow_private_network?}`
  → the webhook and its `secret`, `GET` / `PUT` / `DELETE /admin/webhooks/<id>`,
  `POST /admin/webhooks/<id>/rotate-secret`, `POST /admin/webhooks/<id>/ping`.
* `GET /admin/webhooks/<id>/deliveries`, `GET …/deliveries/<id>` (with the
  payload), `POST …/deliveries/<id>/redeliver`.

Events: `page.created`, `page.updated`, `page.moved`, `page.renamed`,
`page.deleted`, `page.restored`, `category.created`, `category.deleted`,
`user.created`, `user.renamed`, `user.deleted`, `user.role_changed`,
`user.suspended`, `kanban.board.created`, `kanban.board.updated`,
`kanban.board.deleted`, `kanban.ticket.created`, `kanban.ticket.updated`,
`kanban.ticket.moved`, `kanban.ticket.deleted`, `kanban.comment.created`,
`canvas.created`, `canvas.updated`, `canvas.deleted` (and `ping` for tests).
At most 20 webhooks.

Kanban and canvas payloads:

| Event | `data` |
|---|---|
| `kanban.board.*` | `board {id, title, visibility}`, `actor_id` |
| `kanban.ticket.created` / `updated` / `deleted` | `ticket {id, board_id, column_id, title}`, `board`, `actor_id` |
| `kanban.ticket.moved` | the same plus `from_column_id`, `to_column_id` |
| `kanban.comment.created` | `comment {id, ticket_id, user_id}`, `ticket`, `board`, `actor_id` |
| `canvas.*` | `canvas {id, slug, title, visibility}`, `actor_id` |

Ticket descriptions, comment bodies and canvas content are never sent. For
a **private** board or canvas only ids and `visibility` are sent: the board
title, its tickets' titles and a canvas's title and slug are `null`, so a
receiver learns that something changed but not what it is called. Boards
and canvases that are shared or public carry their names. `canvas.updated`
is sent at most once per save.

Each delivery is a `POST` with a JSON body
`{"id": "<uuid>", "event": "page.updated", "created_at": "…Z", "data": {…}}`.
`data` holds ids, slugs, titles, usernames and roles, never page content.
Headers: `X-BananaWiki-Event`, `X-BananaWiki-Delivery` (the `id`),
`X-BananaWiki-Timestamp` (Unix seconds) and
`X-BananaWiki-Signature: sha256=<hex>`, the HMAC-SHA256 with the webhook's
secret of `<timestamp>.<raw body>`. Check it in constant time and reject old
timestamps (`bananawiki.sdk.client.verify_webhook` does both).

Deliveries are attempted right after the request that caused them, by a
short-lived background thread (two at most per worker process, 20 deliveries
each) that does not delay the answer; deliveries for a webhook whose last
attempt failed wait for the `api_service.webhooks` job (about every 30
seconds), which is also the retry path. Each delivery is claimed before it is
sent, so the thread and the job never send it twice. Any non-2xx answer or
network error is retried after 1 minute, 5 minutes, 30 minutes, 2 hours and 6
hours, then marked `failed`; a webhook that fails is skipped for the rest of a
run so it cannot hold up the others. Requests go through
`bananawiki.core.http`: the host is resolved and checked before connecting,
private and loopback addresses are refused unless the webhook allows the local
network, link-local and cloud metadata addresses always, redirects are not
followed, time and answer size are bounded. Under managed hosting a webhook
can never allow the local network (the host's): `allow_private_network: true`
is refused with 403 `private_network_managed`, the admin form does not offer
it, and a flag stored earlier is ignored (and reported as `false`). A refused
destination fails at once. Secrets are stored encrypted with the instance key
(`api_service__webhooks.secret`); moving to another key makes deliveries fail
with `secret_unreadable` until the secret is rotated. The delivery log keeps
30 days. Webhooks work while the REST API feature is on, whether or not the
API switch is.

A webhook is **switched off automatically** after
`api_service_webhook_max_failures` failed attempts in a row (default 20,
`0` never; set it on the admin page or through `PUT /settings`), or when its
deliveries have kept failing for 3 days without a success. It then has
`active: false`, `disabled_reason: "failures"` and `disabled_at`;
administrators see it under *Needs your attention* (and in attention emails)
and on the admin page. Setting `active` (either way) clears the reason;
enabling it also resets `consecutive_failures` and `failing_since`, and its
pending deliveries are sent again.

## Userbots (`userbot`)

An account can turn on **automation mode** on its token page. That issues a
key (a token named `userbot` with read/write `userbot` scope), marks the
account as automated (`users.userbot_enabled`) and counts the change. The key
does not count towards the account's token limit. Administrators can lock the
mode on or off (`users.userbot_mode_lock`); while it is locked on, the owner
can still issue a new key. Only the key carries the `userbot` scope, and the
endpoints answer only while automation mode is on (otherwise 403
`userbot_disabled`), so locking it off stops every script.

* `GET /userbot/me` – account, lock and counters, profile, key timestamps.
* `POST /userbot/profile {real_name?, bio?, page_published?}` – needs write
  access and `profile.edit_own`; publishing is refused while an administrator
  has disabled the profile.

Two clients without dependencies ship with the wiki.
`bananawiki/sdk/client.py` has `WikiClient`: pages, search, kanban, canvas,
paging with `iter_all`, `Idempotency-Key` on creates, `If-Match` through
`revision=`/`version=`, answer headers in `last_headers`, and
`verify_webhook`. `bananawiki/sdk/userbot.py` has `UserbotClient` for the
userbot calls (scripts written for 1.4 can keep importing it from
`bananawiki_userbot_sdk`):

```python
from bananawiki.sdk.userbot import UserbotClient

bot = UserbotClient("https://wiki.example.org", "YOUR_KEY")
print(bot.me())
bot.update_profile(real_name="Bot", bio="Automated", page_published=True)
```

## Audit log

Every authenticated call is recorded in `api_service__audit_log`: account,
token, method, path, status, duration and IP address. Request bodies are
never stored as sent: write requests keep a short summary where values of
keys that look like credentials read `[redacted]`, page content is reduced to
its length and long values are cut (2000 characters at most). Entries older
than a year are removed by the hourly `api_service.prune` job; superusers can
clear older entries sooner on the admin page. Entries written by 1.4 may
still contain request bodies.

## Upgrading from 1.4

What changed for API clients written for 1.4 is listed in
[UPGRADING](../UPGRADING.md#upgrading-from-14), under *REST API*.
