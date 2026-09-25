# Banana Mode API

Banana Mode is part of the built-in **API Service** plugin. The plugin is
pre-installed but disabled by default; enable it in **Admin -> Plugins** to make
the API pages and `/api/v1/*` endpoints available.

## Management

- Admin UI: `/admin/api-service#banana-mode`
- Token UI: `/settings/api-tokens`
- Legacy `/admin/banana` and `/settings/api` URLs redirect to the unified API
  Service pages while the plugin is enabled.

API tokens are stored in `api_service__tokens`, use per-instance HMAC-SHA256
hashing, and are shown only once when created.

## Endpoints

All Banana Mode automation uses Bearer tokens from the API Service plugin.

| Method | Endpoint | Description | Scope |
|---|---|---|---|
| `GET` | `/api/v1/banana-mode` | Read current Banana Mode state | `admin` |
| `POST` | `/api/v1/banana-mode` | Toggle Banana Mode on or off | `admin` + write |

The scope alone is not enough: the account that owns the token must also be
an admin or an owner. A token of any other account gets `403`, even when it
was created with the `admin` scope.

Header:

```http
Authorization: Bearer <token>
```

Example:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://wiki.example.com/api/v1/banana-mode

curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  https://wiki.example.com/api/v1/banana-mode
```

The legacy `/api/banana`, `/api/banana/on`, and `/api/banana/off` endpoints have
been removed.
