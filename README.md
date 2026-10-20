<img src="bananawiki/wiki/static/favicons/apple-touch-icon.png" alt="BananaWiki logo" width="90" height="90">

# BananaWiki

BananaWiki is a self-hosted wiki for teams, schools and communities: Markdown
pages with history, fine-grained permissions, and optional collaboration
tools (chat, kanban boards, canvases, quizzes, read aloud). It runs as a
single wiki, as a desktop app on one computer, or as a hosting platform that
gives each customer a separate wiki.

[Italiano](README.it.md) · [Screenshots](https://bananawiki.com/#screenshots) ·
[Documentation](docs/README.md) · [Getting started](docs/getting-started.md) ·
[Upgrading](UPGRADING.md) · [User guide](docs/user-guide/en/README.md)

## Features

* **Pages**: Markdown editor with toolbar, live preview, image upload with
  alternative text, size, alignment and crop, tables, video embeds and
  `@mentions`; categories with drag-and-drop ordering and sequential
  navigation; full-text search; edit-conflict detection; page history with
  differences, restore and attribution; autosaved drafts; attachments; PDF and
  Markdown export; bulk Markdown import and export; a block-based page
  builder.
* **Access control**: roles (user, editor, administrator, owner), custom
  roles, per-user permissions, read and write access per category, public
  mode, invite codes, open sign-up with an end date, approval of new accounts,
  two-step verification, suspensions, session management, audit log.
* **Content governance**: page protection, check-outs with quotas, proposed
  edits with review, a 48-hour grace period for deletions, scheduled deletion
  of pages and accounts, temporary roles.
* **Collaboration**: direct messages and group chats with retention rules,
  kanban boards, visual canvases, quizzes attached to pages, announcements,
  badges, a contributor leaderboard, member profiles with custom fields.
* **Read aloud**: audio versions of pages with local neural voices (Piper) or
  a GPU server.
* **Integration**: a REST API with scoped tokens and an OpenAPI description,
  federation between wikis, custom pages at any free address, third-party
  plugins (off by default; only the owner or a superuser can install them;
  1.4 plugins load through a compatibility layer).
* **Administration**: appearance themes, interface languages (English,
  Italian and German included, more can be uploaded), built-in user guide,
  whole-site export and import, bulk delete.
* **Operations**: one-command installation with HTTPS configuration, updates
  with a backup and automatic rollback, encrypted backups to a private Git
  repository, Docker Compose, a desktop launcher for Windows, macOS and Linux.
* **Hosting platform**: sign-up, per-customer wikis in isolated containers,
  custom domains, collaborators, quotas and expiry, platform sign-in, its own
  API, Google Drive backups. Each new wiki gets its HTTPS certificate right
  away, also behind Cloudflare, and wikis that stopped are started again.

## Quick start

**From source** (Python 3.11 or newer, Git):

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Open <http://127.0.0.1:5001>, run `bananawiki setup-token` in a second
terminal (same folder, same virtual environment) and enter the token to
create the first account. See [getting started](docs/getting-started.md).

**Docker Compose** (one wiki behind Caddy with automatic HTTPS; building the
image needs Docker's BuildKit component, `docker buildx`):

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=you@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

**Managed Linux server** (systemd services, HTTPS, updates, backups). Install
from the latest release tag:

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
git checkout "$(git tag -l 'v*' | sort -V | tail -1)"
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email you@example.org
sudo bananawiki setup-token
```

Use `--mode hosting` for the hosting platform (needs Docker with BuildKit).
Any filesystem works; on XFS mounted with `prjquota` every wiki also gets a
hard storage limit. Automatic updates stay off until you run
`sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS`. See
[deployment](docs/deployment.md) and [operations](docs/operations.md).

**Desktop**: [BananaWiki Desktop](docs/desktop.md) runs a wiki on a classroom
or office computer without a server.

Upgrading from 1.6 or 1.4: read [UPGRADING.md](UPGRADING.md) first.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -n 2
python -m ruff check .
```

Use `python -m pytest`, not `pytest`: the tests import each other as a
package and need the repository root on the path. CI runs the suite on
Python 3.11 to 3.14.

## Documentation

[docs/](docs/README.md) covers configuration, deployment, operations,
security, permissions, features, the API, federation, plugins, hosting, the
desktop app, read aloud and the architecture, plus a user guide in English
and Italian. Contributors start with [CONTRIBUTING.md](CONTRIBUTING.md) and
[ARCHITECTURE.md](ARCHITECTURE.md). Please follow the
[code of conduct](CODE_OF_CONDUCT.md). Report security issues as described
in [SECURITY.md](SECURITY.md), not in public issues.

## License

BananaWiki is licensed under the GNU Affero General Public License version 3
only (`AGPL-3.0-only`); see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Personal and commercial use are permitted.

If you modify BananaWiki and let people use it over a network, AGPL section
13 requires you to offer them the corresponding source. Set `BW_SOURCE_URL`
to the source of the version you run; the wiki links to it at `/source`.
Keep the third-party notices.

Started in February 2026 by Luca Zani
([OverloadedTech](https://github.com/OverloadedTech)); see [NOTICE](NOTICE).

Copyright © 2026 Luca Zani and contributors. Each contributor keeps the
copyright in their contributions.
