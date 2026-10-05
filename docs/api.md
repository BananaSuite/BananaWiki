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
| `pages` | pages, search, history |
| `categories` | categories |
| `tokens` | the caller's own tokens |
| `users` | accounts (administrators only) |
| `settings` | site settings (administrators only) |
| `admin` | all tokens, API access, audit log (administrators only) |
| `userbot` | `/userbot/*` |

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

`code` is stable and meant for programs; `error` is translated into the
caller's language. Some errors add fields: `field` (invalid input),
`scope`/`write` (`scope_missing`), `revision` (`edit_conflict`), `invalid`
and `refused` (settings).

| Status | Meaning |
|---|---|
| 400 | invalid input (`invalid_input`, `category_missing`, …) |
| 401 | missing, invalid, revoked or expired token |
| 403 | the token or its owner may not do this |
| 404 | not found, or not readable by the caller |
| 409 | conflict: slug taken, page protected or checked out, edit conflict, already pending deletion, `Idempotency-Key` request running or not replayable |
| 422 | `Idempotency-Key` reused for a different request or by another token |
| 413 | body larger than 2 MiB |
| 429 | rate limit (per account, per minute; `Retry-After: 60`) |
| 503 | API switched off, or maintenance mode |

Timestamps are ISO 8601 in UTC (`2025-01-31T09:30:00Z`). Request bodies must
be JSON objects. Booleans are JSON `true`/`false` (`0`/`1` are accepted);
strings like `"false"` are refused.

## Retrying safely (`Idempotency-Key`)

A `POST` sent with `Idempotency-Key: <1–255 visible ASCII characters>` runs
once. Retrying it with the same key, method, path and body within 24 hours
returns the stored answer with `Idempotent-Replayed: true` instead of running
it again. A key is unique per account and bound to the token that first used
it: the same key with a different request, or from another token, is refused
with 422 `idempotency_key_reused`; a retry while the first request is still
running gets 409 `idempotency_in_progress`. Server errors and 429 answers are
not stored, so those requests can simply be retried.

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
| `GET /pages?category_id=&limit=&offset=` | pages the caller can read (`category_id=none` for uncategorised) |
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
* With Deletion Slowdown on, `DELETE` answers 202 and starts the grace
  period.
* A new page's slug comes from `slug` or the title; an existing slug is a
  409 (so "create, and update on 409" works as before).
* `expected_revision` turns a stale update into 409 `edit_conflict`.
* Creating in a category that does not exist answers 400.

Bulk operations validate every item before writing anything, then report
per-item problems in `errors`.

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
pages are listed by slug in `pages`. With Deletion Slowdown on, the answer is
202 and `pending_deletion` lists the scheduled pages; they, and pages another
feature keeps (`kept`), lose the category.

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
  never exceed the calling one — no flag or scope it lacks, no later expiry;
  an omitted expiry inherits the caller's. An expiry is at most 10 years
  ahead (400 `expiry_too_far`), on the token page too. Each token is
  independent: revoking the parent does not revoke its children.
* `GET /admin/tokens`, `POST /admin/tokens/<id>/revoke` (the tokens of a
  superuser, an owner or another administrator only as the hierarchy allows),
  `PUT /admin/users/<id>/api-access {enabled}`,
  `GET /admin/audit-log?limit=&offset=`,
  `DELETE /admin/audit-log?before_days=` (superusers).

## Userbots (`userbot`)

An account can turn on **automation mode** on its token page. That issues a
key (a token named `userbot` with read/write `userbot` scope), marks the
account as automated (`users.userbot_enabled`) and counts the change.
Administrators can lock the mode on or off (`users.userbot_mode_lock`); while
it is locked on, the owner can still issue a new key.

* `GET /userbot/me` – account, lock and counters, profile, key timestamps.
* `POST /userbot/profile {real_name?, bio?, page_published?}` – needs write
  access and `profile.edit_own`; publishing is refused while an administrator
  has disabled the profile.

A small client without dependencies lives in `bananawiki/sdk/userbot.py`
(scripts written for 1.4 can keep `from bananawiki_userbot_sdk import
UserbotClient`: that module ships at the top of the source tree and forwards
to the new client):

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

## Changes from 1.4

* Banana Mode and its endpoints (`/api/v1/banana-mode`, `/admin/banana…`)
  are gone; use maintenance mode.
* New: `GET /pages/<slug>/history`, `GET /history/<id>`, `GET /search`,
  `GET /categories/<id>`, `sequential_nav` on categories, `expected_revision`,
  `?category_id=/limit/offset` on the page list, `/openapi.json`.
* Error bodies carry `ok` and `code` besides `error`; timestamps are ISO 8601
  UTC.
* Deleting a category needs `category.delete` (it was administrators only);
  the title/category of a page needs `page.edit_metadata`; userbot profile
  updates need `profile.edit_own`.
* The userbot key no longer counts towards the token limit, and an
  administrator's "lock on" no longer creates a key nobody can see.
* The rate limit is per account for all its tokens together.
