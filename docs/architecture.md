# Architecture

The contract for anyone changing BananaWiki is
[ARCHITECTURE.md](../ARCHITECTURE.md) at the top of the repository: package
layout, the request pipeline, the database API, how features are declared,
events, template slots, interceptors, background jobs, front-end rules and the
review rules. This page is a map of the repository around it.

## Repository

| Path | What it is |
|---|---|
| `bananawiki/core/` | Shared infrastructure: typed environment, SQLite connections and migrations, crypto, passwords, timestamps, CSRF/CSP/headers, rate limiters, translations, static assets, the outbound HTTP client, outgoing email and the notification schedule. |
| `bananawiki/core/static/css/bananawiki.css` | The wiki and portal's shared design system, served by both at `/static/css/bananawiki.css`. |
| `bananawiki/wiki/` | The wiki application (`create_app()`), with one package per feature in `features/`. |
| `bananawiki/hosting/` | The hosting portal, its maintenance service and the runtime boundary. |
| `bananawiki/ops/` | The `banana` lifecycle controller (standard library only), the runtime agent, Gunicorn settings, the tenant entry point. |
| `bananawiki/desktop/` | The desktop launcher. |
| `bananawiki/sdk/` | The userbot client and the 1.4 plugin SDK adapter. |
| `bananawiki/cli.py` | The `bananawiki` command. |
| `banana` | Entry point of the lifecycle controller (`sudo ./banana install …`). |
| `wsgi.py`, `gunicorn.conf.py` | `gunicorn -c gunicorn.conf.py wsgi:app`, the command every deployment runs. |
| `hosting/wsgi.py`, `hosting/gunicorn.conf.py`, `hosting/maintenance.py` | The portal's entry points (the names 1.4 systemd units use). |
| `scripts/tts_worker.py` | The read-aloud worker process. |
| `bananawiki_sdk/`, `bananawiki_userbot_sdk/` | 1.4 import names, forwarding to `bananawiki.sdk`. |
| `Dockerfile`, `compose.yaml` | Single-wiki image and Compose setup. |
| `Dockerfile.tenant` | Tenant image of the hosting platform. |
| `deploy/` | Caddy examples and the systemd units the controller writes. |
| `contrib/tts-gpu-server/` | The GPU speech server. |
| `packaging/desktop/` | PyInstaller build of the desktop launcher. |
| `tests/` | The test suite (see [CONTRIBUTING](../CONTRIBUTING.md)). |

The root entry points stay where 1.4 had them because the 1.4 updater writes
those exact commands into the systemd units of servers it updates.

Shared presentation rules live in the core stylesheet. Within each application,
use small template helpers for repeated controls: editor action rows live in
`wiki/features/pages/templates/pages/_editor_actions.html`, while hosting fields
live in `hosting/templates/hosting/_macros.html`. Keep permissions, form actions
and page-specific fields visible in the templates that own them.

## In one paragraph

A request passes the pipeline in `bananawiki/wiki/app.py` (stateless views,
rate limit, bearer API, session, setup gate, access decorators, account
state, maintenance, forced steps, CSRF), reaches a feature's blueprint, which
checks permissions and the object, and calls the feature's service. Services
use one database connection per request (`bananawiki.wiki.db.db`), write in
transactions, and emit events after commit; other features react to events
and fill template slots, so features do not import each other's internals
except the shared services (pages, categories, accounts, auth, settings,
storage, markdown). The schema is versioned (`PRAGMA user_version`): 1.4
databases are version 3, version 4 is the 1.6 takeover migration plus
each feature's `schema.py`, and version 5 adds the durable chat upload usage
ledger. Later changes use numbered migrations so already upgraded databases
receive them too.

## Why things are the way they are

* [1.4 review](1.4-review.md): what was wrong in 1.4 and how 1.6 addresses
  each item.
* [Security](security.md): the trust model and the mechanisms.
* `bananawiki/ops/RUNTIME_AGENT.md`: the privilege split of the hosting
  platform.
