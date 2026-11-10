<img src="bananawiki/wiki/static/favicons/apple-touch-icon.png" alt="BananaWiki logo" width="90" height="90">

# BananaWiki

BananaWiki is a self-hosted wiki for teams, schools and communities. It has
Markdown pages with history, fine-grained permissions and optional
collaboration tools such as chat, kanban boards, canvases, quizzes and read
aloud. You can run it as a single wiki, as a desktop app on one computer, or
as a hosting platform that gives each customer their own wiki.

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
  two-step verification, suspensions, session management and an audit log.
* **Content governance**: page protection, check-outs with quotas, proposed
  edits with review, a 48-hour grace period for deletions, scheduled deletion
  of pages and accounts, and temporary roles.
* **Collaboration**: direct messages and group chats with retention rules,
  kanban boards, visual canvases, quizzes attached to pages, announcements,
  badges, a contributor leaderboard and member profiles with custom fields.
* **Read aloud**: audio versions of pages, using local neural voices (Piper)
  or a GPU server.
* **Integration**: a REST API with scoped tokens and an OpenAPI description,
  federation between wikis, custom pages at any free address, and third-party
  plugins. Plugins are disabled by default and only the owner or a superuser
  can install them. Plugins written for 1.4 still load through a
  compatibility layer.
* **Administration**: appearance themes, interface languages (English,
  Italian and German are included, and you can upload more), a built-in user
  guide, whole-site export and import, and bulk delete.
* **Operations**: one-command installation with HTTPS, updates with a backup
  and automatic rollback, encrypted backups to a private Git repository,
  Docker Compose, and a desktop launcher for Windows, macOS and Linux.
* **Hosting platform**: sign-up, a separate wiki in its own container for each
  customer, custom domains, collaborators, quotas and expiry, platform
  sign-in, its own API and Google Drive backups. New wikis get an HTTPS
  certificate right away, including behind Cloudflare, and stopped wikis are
  restarted automatically.

## Quick start

### From source

You need Python 3.11 or newer and Git.

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Then open <http://127.0.0.1:5001>. To create the first account, run
`bananawiki setup-token` in a second terminal, in the same folder and virtual
environment, and enter the token it prints. For more details, see
[Getting started](docs/getting-started.md).

### Docker Compose

This runs one wiki behind Caddy with automatic HTTPS. Building the image
requires Docker BuildKit (`docker buildx`).

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=you@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

### Linux server

The installer sets up systemd services, HTTPS, updates and backups. Install
from the latest release tag:

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
git checkout "$(git tag -l 'v*' | sort -V | tail -1)"
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email you@example.org
sudo bananawiki setup-token
```

To install the hosting platform instead, use `--mode hosting`. This requires
Docker with BuildKit. Any filesystem works, but on XFS mounted with
`prjquota` each wiki also gets a hard storage limit. Automatic updates are
off until you turn them on with
`sudo bananawiki updates enable --require-signatures ALLOWED_SIGNERS`. For
more details, see [Deployment](docs/deployment.md) and
[Operations](docs/operations.md).

### Desktop

[BananaWiki Desktop](docs/desktop.md) runs a wiki on a classroom or office
computer, without a server.

If you're upgrading from 1.6 or 1.4, read [UPGRADING.md](UPGRADING.md) first.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q -n 2
python -m ruff check .
```

Run the tests with `python -m pytest` rather than `pytest`. The tests import
each other as a package, so the repository root has to be on the path. CI
runs the test suite on Python 3.11 to 3.14.

## Documentation

The [documentation](docs/README.md) covers configuration, deployment,
operations, security, permissions, features, the API, federation, plugins,
hosting, the desktop app, read aloud and the architecture. It also includes a
user guide in English and Italian.

If you'd like to contribute, start with [CONTRIBUTING.md](CONTRIBUTING.md)
and [ARCHITECTURE.md](ARCHITECTURE.md), and please follow the
[code of conduct](CODE_OF_CONDUCT.md). To report a security issue, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Licence

BananaWiki is licensed under the [GNU Affero General Public License v3.0
only](LICENSE) (`AGPL-3.0-only`). You can use it for personal and commercial
purposes.

If you run a modified version of BananaWiki that other people use over a
network, the AGPL requires you to make your source code available to them.
Set `BW_SOURCE_URL` to the address of your source code, and the wiki will link
to it at `/source`. If you redistribute BananaWiki, keep the third-party
notices listed in [NOTICE](NOTICE).

BananaWiki was created by Luca Zani
([OverloadedTech](https://github.com/OverloadedTech)) in February 2026.

Copyright © 2026 Luca Zani and contributors. Contributors keep the copyright
to their contributions.
