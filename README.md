<img src="bananawiki/wiki/static/favicons/banana_yellow.png" alt="BananaWiki logo" width="64">

# BananaWiki

BananaWiki is a self-hosted wiki for teams, schools and communities: Markdown
pages with history, fine-grained permissions, and optional collaboration
tools (chat, kanban boards, canvases, quizzes, read aloud). It runs as a
single wiki, as a desktop app on one computer, or as a hosting platform that
creates a separate wiki for each customer.

[Italiano](README.it.md) · [Documentation](docs/README.md) ·
[Getting started](docs/getting-started.md) · [Upgrading from 1.4](UPGRADING.md) ·
[User guide](docs/user-guide/en/README.md)

## Features

* **Pages**: Markdown editor with toolbar, live preview, image upload, tables,
  video embeds and `@mentions`; categories with drag-and-drop
  ordering and sequential navigation; full-text search; edit-conflict
  detection; full page history with differences, restore and attribution;
  autosaved drafts; attachments; PDF and Markdown export; bulk Markdown import
  and export.
* **Access control**: roles (user, editor, administrator, owner), custom
  roles, per-user permissions, read and write access per category, public
  mode, invite codes, open sign-up with an end date, approval of new accounts,
  suspensions, session management, audit log.
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
  plugins (1.4 plugins still load).
* **Administration**: appearance themes, interface languages (English and
  Italian included, more uploadable), built-in user guide, whole-site export
  and import, bulk delete.
* **Operations**: one-command installation with automatic HTTPS
  configuration, updates with backup and automatic rollback, encrypted
  backups to a private Git repository, Docker Compose, a desktop launcher for
  Windows, macOS and Linux.
* **Hosting platform**: sign-up, per-customer wikis in isolated containers,
  custom domains, collaborators, quotas and expiry, platform sign-in, its own
  API, Google Drive backups.

## Quick start

**Try it from source** (Python 3.11 or newer):

```sh
git clone https://github.com/OverloadedTech/BananaWiki.git
cd BananaWiki
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
bananawiki serve
```

Open <http://127.0.0.1:5001>, run `bananawiki setup-token` in a second
terminal and enter the token to create the first account. Details:
[getting started](docs/getting-started.md).

**Docker Compose** (a wiki behind Caddy with automatic HTTPS):

```sh
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=you@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

**Managed Linux server** (systemd services, HTTPS, updates, backups):

```sh
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy --install --email you@example.org
sudo bananawiki setup-token
```

Use `--mode hosting` for the hosting platform. Automatic updates stay off
until `sudo bananawiki updates enable`. See [deployment](docs/deployment.md)
and [operations](docs/operations.md).

**Desktop**: [BananaWiki Desktop](docs/desktop.md) runs a wiki on a classroom
or office computer without a server.

Upgrading a 1.4 installation: [UPGRADING.md](UPGRADING.md).

## Documentation

Everything is indexed in [docs/](docs/README.md): configuration, deployment,
operations, security, permissions, features, API, federation, plugins,
hosting, desktop, read aloud, architecture, the user guide in English and
Italian, and a [review of what was wrong in 1.4](docs/1.4-review.md).
Contributors start with [CONTRIBUTING.md](CONTRIBUTING.md) and
[ARCHITECTURE.md](ARCHITECTURE.md); security reports go through
[SECURITY.md](SECURITY.md).

## Background

Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) started
BananaWiki alone on 20 February 2026, when Canalescuola needed a wiki for the
Officina Tecnologica project. Work sped up in June 2026 during an FSL
placement (formazione scuola-lavoro, the Italian school work experience
scheme), and since then Luca and Officina Tecnologica have maintained it.
Over those months one wiki for one group turned into a platform that
provisions and runs a wiki per tenant, with the approvals, quotas and custom
domain handling that implies.

Until September 2026 it was an internal project. The first public release
turned that internal version into free software: the same code runs the
hosted service, and anyone can run it on their own server instead. Version
1.6 is a rewrite of that release, keeping its data, URLs and configuration
so existing installations upgrade in place.

The code was developed privately for about seven months, and the internal
history runs to a little over three thousand commits. None of it is
published: it carries deployment credentials and notes about infrastructure,
and there is no way to remove every secret from that many commits with
certainty. The public repository starts from a clean export instead;
[NOTICE](NOTICE) keeps the original start date and the first commit hash on
record.

## License and ownership

BananaWiki is licensed under the GNU Affero General Public License version 3
(`AGPL-3.0-only`); see [LICENSE](LICENSE). Personal and commercial use are
permitted. There are no registration keys or commercial-use declarations.

If you modify BananaWiki and let people use it over a network, AGPL section
13 requires you to offer them the corresponding source. Set `BW_SOURCE_URL`
to the source of the version you run; the wiki links to it at `/source`.
Keep third-party notices.

Copyright © 2026 Luca Zani and all contributors. Each contributor retains
copyright in their contributions. Source: <https://github.com/OverloadedTech/BananaWiki>.
