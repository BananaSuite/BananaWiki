# Hosting REST API

The hosting portal has a small JSON API under `/api/v1` on the portal's own
hostname. Customers authenticate with personal access tokens they create on
their Account page. Version 1 reads account and wiki details and pauses or
resumes a wiki; platform administrators can also read the approval queue and
instance totals. It is off until an administrator switches it on.

This is separate from the [wiki's own API](../../docs/api.md), which each
hosted wiki serves on its own address with its own tokens.

## Switching it on

Open Platform Settings as a platform administrator, tick **Enable the REST
API** in the REST API section and save. The setting is off on new platforms
and on existing ones after an upgrade.

While it is off:

- every path under `/api/v1` answers `404` with a JSON body, including
  `/api/v1/status`;
- the Account page offers no way to create a token, and the form that
  creates tokens answers `404`. An account that still has tokens keeps
  seeing them there, so it can revoke them.

Switching it off does not revoke tokens. They work again when the API is
switched back on, unless they have expired or been revoked in the meantime.

The first start of a version with the API upgrades the hosting database to
schema version 2, which adds the `api_enabled` setting, the
`hosting_api_tokens` table and the settings for signup approval
notifications. That happens whether or not the API is used. An
older version refuses to start against the upgraded database, so keep the
backup taken before updating if you might roll back.

## Tokens

A customer creates a token under **Account > API tokens** by giving it:

- a name of 1 to 60 characters, only as a reminder of where it is used;
- one or more scopes (see below);
- an expiry: 30, 90 or 365 days, or no expiry;
- the account's current password.

The page then shows the token once. It looks like `bwh_` followed by 40
URL-safe characters. The portal renders it straight into that response and
does not keep it anywhere, not even in the session: the database stores only
the SHA-256 digest of the whole token and its first 12 characters, which the
Account page shows so tokens can be told apart. A lost token cannot be
recovered; revoke it and create another.

Other rules:

- An account can hold 10 active tokens. Expired and revoked tokens do not
  count.
- Each rendering of the form can create one token. Reloading the page that
  shows a new token, or sending the same form twice, does not create a
  second one.
- If the account's password changes, or all of its sessions are ended,
  between the password check and the moment the token is stored, no token
  is created and the customer has to log in again.
- An account that still has to verify its contact email sees its tokens
  and can revoke them, but cannot create new ones.
- Tokens cannot be created while an administrator is impersonating the
  account. The administrator can still revoke them, and is then recorded as
  the one who did.
- Changing the password on the Account page, resetting it through the
  emailed link, and a new password set by an administrator or with
  `python -m hosting.admin_cli account set-password` all revoke every token
  of the account.
- Deleting an account deletes its tokens. Merging an account into another
  revokes the tokens of the account that is merged away.
- `admin:read` is offered only to platform administrators, and the API checks
  the administrator flag again on every request, so a token keeps working
  for the other scopes after its owner is demoted but loses `admin:read`.

Creating and revoking a token, and every pause or resume made through the API,
is written to the portal's moderation history (`hosting_events`) with the
token's id and prefix: `api.token.created`, `api.token.revoked`,
`api.instance.paused` and `api.instance.resumed`. The token itself is never
logged.

## Authentication

Send the token on every request:

```
Authorization: Bearer bwh_...
```

That header is the only credential the API reads. A signed-in browser session
is ignored, so a portal cookie cannot authenticate an API call. Because a
browser never attaches a bearer token by itself, the API routes, and only
they, are exempt from the portal's CSRF protection. The API sends no CORS
headers, so a page on another origin cannot read its responses.

Every request checks again that:

1. the API is switched on;
2. the token exists, has not been revoked and has not expired;
3. the account exists and is not suspended (a timed suspension that has run
   out is lifted, as it would be on the dashboard);
4. the account is not scheduled for deletion, its contact email has not been
   flagged as invalid, and, where the platform requires verified emails, that
   email is verified;
5. the account has been approved;
6. the token carries the scope the endpoint needs, and for `admin:read` the
   account is still a platform administrator.

A successful check records the time as the token's last use.

## Scopes

| Scope | Allows |
| --- | --- |
| `account:read` | `GET /me` |
| `instances:read` | `GET /instances` and `GET /instances/<id>` |
| `instances:manage` | `POST /instances/<id>/pause` and `POST /instances/<id>/resume` |
| `admin:read` | `GET /admin/pending-accounts` and `GET /admin/instances`; platform administrators only |

Tokens act for their account as a customer. Instance endpoints reach the wikis
the account owns and the ones shared with it, and apply collaborator
permissions exactly as the dashboard does: a collaborator needs **View** to
see a wiki and **Start Stop** to pause or resume it. An administrator's token
does not reach other people's wikis.

## Endpoints

The examples use `https://portal.example.com` for the portal and assume the
token is in `$TOKEN`. Responses are JSON with keys in alphabetical order.
Timestamps are ISO 8601 in UTC, as stored.

### Instance object

```json
{
  "created_at": "2026-09-01T10:12:03.418220+00:00",
  "expires_at": "2026-09-15T10:12:03.418220+00:00",
  "id": "q7x2m9k4w1c8z5n3",
  "role": "owner",
  "status": "running",
  "storage_limit_bytes": 524288000,
  "storage_used_bytes": 18350080,
  "subdomain": "team",
  "url": "https://team-hosting.example.com"
}
```

- `status` is `running`, `stopped` (paused) or `suspended`. Terminated wikis
  are not returned.
- `role` is `owner` or `collaborator`.
- `expires_at` is `null` for a wiki that does not expire, and
  `storage_limit_bytes` is `null` for one without a storage cap.
  `storage_used_bytes` is `null` if the portal could not measure it.
- `url` is `null` if the wiki has no public address.

Nothing else from the instance record is returned: no administrator
credentials or one-time passwords, ports, container details, data paths,
suspension notes or other accounts' details.

### GET /api/v1/status

No token needed. Tells a client the API is switched on.

```sh
curl https://portal.example.com/api/v1/status
```

```json
{"api": "v1"}
```

### GET /api/v1/me

Scope `account:read`. The account and the token making the request.

```sh
curl -H "Authorization: Bearer $TOKEN" https://portal.example.com/api/v1/me
```

```json
{
  "account": {
    "created_at": "2026-08-30T17:40:11.052318+00:00",
    "email": "maria@example.org",
    "id": "k3v9x2m1q8wz",
    "is_admin": false,
    "username": "maria"
  },
  "token": {
    "created_at": "2026-09-20T08:00:00.123456+00:00",
    "expires_at": "2026-12-19T08:00:00.123456+00:00",
    "id": 4,
    "name": "backup script",
    "prefix": "bwh_Qm8sT2aL",
    "scopes": ["account:read", "instances:read"]
  }
}
```

### GET /api/v1/instances

Scope `instances:read`. The account's own wikis, newest first, followed by
the wikis shared with it.

```sh
curl -H "Authorization: Bearer $TOKEN" https://portal.example.com/api/v1/instances
```

```json
{"instances": [{"id": "q7x2m9k4w1c8z5n3", "role": "owner", "status": "running", "...": "..."}]}
```

### GET /api/v1/instances/&lt;id&gt;

Scope `instances:read`. One wiki, as an instance object.

```sh
curl -H "Authorization: Bearer $TOKEN" \
  https://portal.example.com/api/v1/instances/q7x2m9k4w1c8z5n3
```

```json
{"instance": {"id": "q7x2m9k4w1c8z5n3", "status": "running", "...": "..."}}
```

An id that does not exist, belongs to a wiki the account cannot see, or
belongs to a terminated wiki answers `404`, so the API does not reveal
whether someone else's wiki exists.

### POST /api/v1/instances/&lt;id&gt;/pause

Scope `instances:manage`. Stops a running wiki and keeps its files, as the
dashboard's **Pause** button does. Needs no request body.

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://portal.example.com/api/v1/instances/q7x2m9k4w1c8z5n3/pause
```

```json
{"instance": {"id": "q7x2m9k4w1c8z5n3", "status": "stopped", "...": "..."}}
```

- `403` if the account is a collaborator without **Start Stop**, or owns the
  wiki and an administrator has suspended it.
- `409` if the wiki is not running. The body includes its current `status`.

Reading a wiki with `GET` needs the **View** permission, but pause and resume
answer with the full instance object to any collaborator who holds **Start
Stop**, as the dashboard does.

### POST /api/v1/instances/&lt;id&gt;/resume

Scope `instances:manage`. Starts a paused wiki again, as the dashboard's
**Resume** button does.

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://portal.example.com/api/v1/instances/q7x2m9k4w1c8z5n3/resume
```

```json
{"instance": {"id": "q7x2m9k4w1c8z5n3", "status": "running", "...": "..."}}
```

- `403` as for pause, and for anyone while an administrator's suspension
  is recorded on the wiki. A suspended wiki cannot be resumed from the API
  or the dashboard.
- `409` if the wiki is not paused.
- `500` if the wiki could not be started; the body says why.

### GET /api/v1/admin/pending-accounts

Scope `admin:read`. Accounts waiting for approval, newest first. This is the
only place the API returns other people's usernames and email addresses.

```sh
curl -H "Authorization: Bearer $TOKEN" \
  https://portal.example.com/api/v1/admin/pending-accounts
```

```json
{
  "accounts": [
    {
      "created_at": "2026-09-22T14:03:51.772104+00:00",
      "email": "giulia@example.org",
      "email_verified": true,
      "id": "p0z8r6t4y2u1",
      "use_case": "Documentation for our climbing club",
      "username": "giulia"
    }
  ]
}
```

### GET /api/v1/admin/instances

Scope `admin:read`. Instance counts across the platform. `terminated`
includes wikis whose data has already been removed.

```sh
curl -H "Authorization: Bearer $TOKEN" \
  https://portal.example.com/api/v1/admin/instances
```

```json
{
  "by_status": {"running": 41, "stopped": 6, "suspended": 1, "terminated": 12},
  "total": 60
}
```

## Errors

Every error is JSON with an `error` message:

```json
{"error": "This token does not have the instances:manage scope."}
```

| Status | When |
| --- | --- |
| `401` | No bearer token, or one that is malformed, unknown, revoked or expired. Also carries `WWW-Authenticate: Bearer`. The message does not say which. |
| `403` | The token lacks the scope, the account is suspended, pending deletion, unapproved or has a flagged or unverified email, `admin:read` without administrator rights, or a collaborator permission or suspension blocks the action. |
| `404` | The API is switched off, the path does not exist, or the wiki is unknown or not visible to the account. |
| `405` | The path exists but not for that method. Carries `Allow`. |
| `409` | The wiki is in the wrong state for the action. Includes `status`. |
| `429` | A rate limit was reached. Includes `retry_after` and the `Retry-After` header, both in seconds. |
| `500` | A resume could not start the wiki, and the message says why, or something unexpected failed on the server. The unexpected kind includes a `request_id` that operators can find in the logs. |
| `503` | The hosting database is temporarily unavailable. Includes `retry_after` and the `Retry-After` header, both in seconds. |

## Rate limits

Limits are counted per token over a sliding minute and stored in the hosting
database, so all portal workers share them:

- 60 requests per minute for all endpoints together;
- 10 pause or resume requests per minute, which also count towards the 60.

Requests without a valid token are not rate limited by the portal. If you
need a limit on them, add one on the reverse proxy in front of the portal,
for example with the `rate_limit` directive of Caddy's rate limit module or
`limit_req` in nginx, applied to `/api/v1/*`.

## What version 1 leaves out

The API cannot terminate or delete a wiki, reset a wiki or its administrator
password, download or export data, create wikis, manage collaborators or
ownership transfers, create or list tokens, or carry out any administrator
action such as approving, denying or suspending accounts.

Those actions are either impossible to undo, hand out credentials or data, or
rely on confirmation steps in the portal such as retyping a password. Leaving
them out keeps the damage a leaked token can do to reading details and pausing
wikis, which can be resumed. They stay in the portal, where they already have
those safeguards.
