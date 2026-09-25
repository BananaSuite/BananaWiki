# Platform OAuth SSO

> **Location:** `hosting/routes/oauth.py` (provider) · `routes/platform_oauth.py` (consumer)
> **Database Migrations:** `hosting/db/_schema.py`
> **Instance Env Injection:** `hosting/instance_manager.py`

The platform OAuth SSO feature lets users sign in to any wiki instance using
their hosting portal credentials. It implements the **Authorization Code Grant**
flow (RFC 6749 §4.1), with the hosting portal acting as the authorization
server and each wiki instance acting as a confidential OAuth client.

---

## Architecture

```
┌──────────────────────┐     1. Login Request       ┌──────────────────────┐
│                      │ ─────────────────────────── │                      │
│   Wiki Instance      │     2. AuthZ Redirect       │  Hosting Portal      │
│   (OAuth Client)     │ ◄─────────────────────────── │  (OAuth Provider)    │
│                      │                             │                      │
│  routes/             │     3. User Authenticates   │  routes/oauth.py     │
│  platform_oauth.py   │     4. Consent               │                      │
│                      │                             │  /oauth/authorize    │
│  Config:             │     5. Auth Code ←           │  /oauth/token         │
│  BW_PLATFORM_OAUTH_* │                             │  /oauth/userinfo      │
│                      │     6. Code → Token          │  /oauth/link          │
│                      │ ─────────────────────────── │  /oauth/unlink        │
│                      │     7. Token → UserInfo      │  /oauth/link-status   │
│                      │ ◄─────────────────────────── │                      │
│                      │                             │  DB:                 │
│                      │     8. Create/Link/Login     │  hosting_oauth_*     │
│                      │                             │  tables              │
└──────────────────────┘                             └──────────────────────┘
```

### Why Authorization Code Grant?

- The client secret is never exposed to the browser: it is exchanged
  server-to-server.
- Short-lived authorization codes (5-minute expiry, single-use) limit
  the window for interception.
- Access tokens are signed with HMAC-SHA256, verifiable without a DB
  round-trip.

---

## Enabling the Feature

1. Sign in to the hosting portal as an admin.
2. Navigate to **Settings** → **Platform OAuth SSO**.
3. Check **Enable platform OAuth SSO** and click **Save**.
4. Restart running wikis so they pick up the new OAuth credentials
   (injected as environment variables at spawn time).

Once enabled, each wiki instance automatically gets:
- A unique `oauth_client_id` / `oauth_client_secret_hash` stored in the
  `instances` table.
- Environment variables injected by `_instance_env()` in
  `hosting/instance_manager.py`:

| Variable | Description |
|---|---|
| `BW_PLATFORM_OAUTH_ENABLED` | `"1"` when active |
| `BW_PLATFORM_OAUTH_CLIENT_ID` | Instance's OAuth client ID |
| `BW_PLATFORM_OAUTH_CLIENT_SECRET` | Instance's raw client secret (injected only at credential creation) |
| `BW_PLATFORM_OAUTH_PORTAL_BASE` | Base URL of the hosting portal |
| `BW_PLATFORM_OAUTH_AUTHORIZE_URL` | Full authorize endpoint |
| `BW_PLATFORM_OAUTH_TOKEN_URL` | Full token endpoint |
| `BW_PLATFORM_OAUTH_USERINFO_URL` | Full userinfo endpoint |
| `BW_PLATFORM_OAUTH_VERIFY_URL` | Full token verification endpoint |
| `BW_PLATFORM_OAUTH_LINK_URL` | Full link-creation endpoint |
| `BW_PLATFORM_OAUTH_UNLINK_URL` | Full unlink endpoint |
| `BW_PLATFORM_OAUTH_LINK_STATUS_URL` | Full link-status endpoint |
| `BW_PLATFORM_INSTANCE_ID` | Instance's ID in the hosting DB |

---

## OAuth Flow (First-Time Login)

### Step-by-Step

1. User visits any wiki and clicks **Log in with Platform Account** on the
   login page.

2. Wiki generates a `state` token (CSRF protection, stored in session,
   10-minute expiry) and redirects to:
   ```
   /oauth/authorize?response_type=code&client_id=...&redirect_uri=...&state=...
   ```

3. If the user is not logged into the hosting portal, they are redirected
   to the portal's login page first. After login, they return to the
   authorize endpoint.

4. The portal shows a **consent page** (`oauth_consent.html`) listing what
   the wiki will access (username, account ID). The user clicks **Authorise**.

5. The portal generates a single-use authorization code and redirects back
   to the wiki:
   ```
   /platform-oauth/callback?code=...&state=...
   ```

6. The wiki verifies the `state` token, then exchanges the code for an
   access token via `POST /oauth/token` (server-to-server, authenticating
   with `client_id` + `client_secret`).

7. The wiki fetches user info via `GET /oauth/userinfo` (Bearer token).

8. The wiki processes the login:
   - **No local user with this username:** creates a new wiki user (role:
     `"user"`) and links it to the hosting account.
   - **Local user exists, already linked to this hosting account:** logs
     the user in directly.
   - **Local user exists, NOT linked:** shows the merge-verification page
     (password required).
   - **Local user exists, linked to a DIFFERENT hosting account:** blocks
     login with a conflict message.

### Merge Verification

When a local wiki user has the same username as the incoming OAuth user but
is not yet linked to any hosting account, the wiki shows a merge form
(`auth/platform_oauth_merge.html`). The user must enter the local wiki
password to prove ownership. On success, the accounts are linked and the
user is signed in.

---

## Account Linking

Users can link their existing wiki account to a hosting account at any time.

### From User Settings

1. Go to **Settings** → **Platform Account** → **Manage Platform Account Link**.
2. If already linked, you will see the linked hosting account username and
   can unlink by entering your wiki password.
3. If not linked, enter your wiki password and click **Link Platform Account**.
   You will be redirected to the portal to authorize the link, then back to
   settings.

### Unlinking

- The unlink form requires the current wiki password.
- Unlinking removes only the association in the
  `hosting_oauth_account_links` table and keeps the wiki user.
- After unlinking, the user can still sign in with their local wiki password.

---

## Security Considerations

| Concern | Mitigation |
|---|---|
| Authorization code interception | Single-use codes, 5-minute expiry, requires `client_secret` to exchange |
| Token forgery | HMAC-SHA256 signed tokens using `HOSTING_SECRET_KEY` |
| Token replay | 1-hour token expiry, optional server-side revocation |
| CSRF on OAuth flow | Random `state` parameter validated on callback |
| CSRF on forms | WTForms / per-form CSRF tokens |
| Brute-force | Rate-limited endpoints (20 req/min for auth, 10 req/min for linking) |
| Client secret leak | Stored as `salt$sha256` in DB; raw secret injected only at credential creation |
| Same-username collision | Password-verified merge; blocked if username already linked to a different account |

### Credential Storage

Client secrets are stored in the `instances` table as:
```
<salt>$<sha256(salt + raw_secret)>
```
The raw secret is returned only once (at credential creation) and must be
injected into the instance environment immediately. The `_get_or_create_instance_oauth_credentials()`
function returns `(client_id, None)` on subsequent calls because the hash
is already stored.

### Token Format

Access tokens use a custom signed format instead of JWT:
```
<urlsafe_b64(payload)>.hmac_hex[:32]
```
- Payload is a compact JSON dict (`{"sub","client_id","iat","exp","scope"}`).
- Signature is HMAC-SHA256 using `HOSTING_SECRET_KEY`, truncated to 32 hex chars.

---

## Database Schema

### Hosting Portal Tables

**`hosting_oauth_authorization_codes`**
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `code` | TEXT UNIQUE | Authorization code |
| `client_id` | TEXT | OAuth client ID |
| `account_id` | TEXT | Hosting account ID |
| `redirect_uri` | TEXT | Original redirect URI |
| `used` | INTEGER | 0 = unused, 1 = consumed |
| `expires_at` | TEXT (ISO-8601) | Expiry timestamp |
| `created_at` | TEXT (ISO-8601) | Creation timestamp |

**`hosting_oauth_access_tokens`**
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `token_hash` | TEXT UNIQUE | SHA-256 of token value |
| `client_id` | TEXT | OAuth client ID |
| `account_id` | TEXT | Hosting account ID |
| `scope` | TEXT | Granted scopes |
| `expires_at` | TEXT (ISO-8601) | Expiry timestamp |
| `created_at` | TEXT (ISO-8601) | Creation timestamp |

**`hosting_oauth_account_links`**
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `instance_id` | TEXT | FK → `instances.id` |
| `account_id` | TEXT | FK → `accounts.id` |
| `wiki_user_id` | TEXT | Wiki's internal user ID |
| `wiki_username` | TEXT | Wiki username (denormalized) |
| `linked_at` | TEXT (ISO-8601) | Link creation timestamp |

UNIQUE constraint on `(instance_id, account_id)`. A hosting account can only
be linked to one wiki user per instance.

### Instances Table Additions

- `instances.oauth_client_id`: unique, nullable OAuth client ID.
- `instances.oauth_client_secret_hash`: nullable salted SHA-256 hash.

---

## Routes Reference

### Hosting Portal (Provider)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET/POST | `/oauth/authorize` | Portal session | Authorization endpoint with consent UI |
| POST | `/oauth/token` | client_secret | Token endpoint (authorization_code) |
| GET | `/oauth/userinfo` | Bearer token | Returns account profile |
| GET | `/oauth/verify` | Bearer token | Validates token metadata |
| GET | `/oauth/link-status` | None (server-to-server) | Checks account link status |
| POST | `/oauth/link` | None (server-to-server) | Creates account link |
| POST | `/oauth/unlink` | None (server-to-server) | Removes account link |
| POST | `/oauth/revoke` | Portal session | Revokes all tokens for current user |
| POST | `/admin/instances/<id>/rotate-oauth-credentials` | Admin | Regenerates client credentials |

### Wiki (Consumer)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/platform-oauth/login` | None | Initiates OAuth flow (redirects to portal) |
| GET | `/platform-oauth/callback` | None | Handles authorization code callback |
| POST | `/platform-oauth/merge` | None | Merge-verification form handler |
| GET | `/settings/link-platform-account` | Login required | Shows link/unlink UI |
| POST | `/settings/link-platform-account` | Login required | Initiates account linking flow |
| GET | `/platform-oauth/link-callback` | None | Handles linking OAuth callback |
| POST | `/settings/unlink-platform-account` | Login required | Unlinks accounts |

---

## Edge Cases

### Username Collision

Scenario: A local wiki user `"alice"` exists and a different hosting
account user `"alice"` tries to sign in via OAuth.

- If `"alice"` is already linked to the OAuth account → success.
- If `"alice"` is already linked to a different hosting account →
  blocked with conflict error.
- If `"alice"` is not linked → merge-verification page requires the
  local password.

### Account Already Linked

If a hosting account is already linked to a different wiki user on the same
instance, the link-creation endpoint (`/oauth/link`) returns HTTP 409.
The wiki shows an error message.

### Platform OAuth Disabled Mid-Flow

If an admin disables OAuth while a user is in the middle of the flow:
- Auth codes and tokens are still valid until they expire (max 1 hour).
- New authorization requests return 404.
- Existing links are preserved but cannot be used for login after the
  wiki restarts (the `BW_PLATFORM_OAUTH_ENABLED` env var will be `"0"`).

### Credential Rotation

Admins can rotate an instance's OAuth credentials. The old credentials
continue to work for existing sessions (access tokens remain valid until
expiry) but new login flows fail. Wikis must be restarted after rotation.

---

## Testing

### Manual Test Procedure

1. Enable OAuth in the hosting portal settings.
2. Restart a wiki instance (or spawn a new one).
3. Open the wiki in a private/incognito browser window.
4. Click **Log in with Platform Account**: you should be redirected to
   the portal.
5. If not logged into the portal, log in. You should see the consent page.
6. Click **Authorise**: you should be redirected back to the wiki and
   signed in as a new user.
7. Sign out and sign in again via OAuth: should work immediately
   (account already linked).
8. **Test merge:** create a local wiki user with the same username, then
   try OAuth login with a different hosting account.
9. **Test linking:** sign in with local credentials, visit settings, and
   link your hosting account.
10. **Test unlink:** unlink and verify that local password login still works.
