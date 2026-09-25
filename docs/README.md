# BananaWiki documentation

Everything in this directory, grouped by what you are trying to do. The
[project README](../README.md) is the shorter entry point.

## Install and run

- [Getting started](getting-started.md): first local wiki, first administrator.
- [Deploy and maintain](deployment.md): the managed `banana` command, HTTPS,
  persistent data, optional automatic updates.
- [One deployment command](unified-deployment.md): short summary of the same
  installer for people who already know what they want.
- [Easy Deployment App](easy-deployment-app.md): the portable desktop launcher.
- [Configuration](configuration.md): static settings in `config.py` and the
  runtime settings stored in the database.
- [Capacity and operating limits](capacity.md): measured workload and how to
  size a deployment.

## Run it in production

- [Operations](operations.md): day to day maintenance on a managed server.
- [Encrypted repository backups](backups.md): private GitHub or Forgejo
  destinations, recovery keys, scheduling.
- [Move to another server](server-migration.md): portable packages and legacy
  imports.
- [Architecture and security](architecture-and-security.md): entry points,
  trust boundaries and the reasoning behind them.

## Hosting platform

- [Hosting platform](hosting.md): provisioning a wiki per tenant.
- [Custom domains](custom-domains.md): subdomain mode, DNS, per-instance
  approval.
- [Platform OAuth SSO](platform-oauth-sso.md): the portal as an identity
  provider for its instances.
- [Hosting REST API](../hosting/docs/api.md): personal access tokens that
  read accounts and wikis and pause or resume them. Off until an administrator
  switches it on.

## Between installations

- [Federation](federation.md): pairing two wikis and sharing single pages
  read only. Off unless `BW_FEDERATION_ENABLED` is set.

## Using the wiki

- [What is BananaWiki?](wiki-overview.en.md) and
  [Cos'è BananaWiki?](wiki-overview.it.md): non-technical overviews.
- [User guide](user-guide/en/README.md) and
  [guida utente](user-guide/it/README.md).
- [Permissions](permissions.md): roles, custom roles, per-category access.
- [Temporary items](temporary-items.md): scheduled expiry for pages, accounts
  and uploads.

## Features that ship as plugins

- [Kanban](kanban.md), [Canvas](canvas.md), [Badges](badges.md),
  [Custom pages](custom-pages.md).
- [PDF export](pdf-export.md): needs the `page_history` plugin enabled.
- [Remote GPU TTS](remote-gpu-tts-guide.md): experimental, routes synthesis to
  a separate GPU host.
- [Obsidian vault sync](OBSIDIAN_SETUP.md): experimental, command line only.

## Build on top of it

- [JSON API](api.md): the endpoints the interface itself uses.
- [Banana Mode API](banana_mode_api.md) and [userbots](userbots.md): automation
  through the API Service plugin.
- [Plugin development](plugins/overview.md), [authoring](plugins/authoring.md)
  and the [plugin API reference](plugins/api-reference.md).

## Working on BananaWiki

- [Development and maintenance](development.md): module layout, tests, the
  source shared with BananaChat.
- [What the scripts do](../scripts/README.md): every tool in `scripts/`.
- [Contributing](../CONTRIBUTING.md), [code of conduct](../CODE_OF_CONDUCT.md)
  and [security reports](../SECURITY.md).
- [Migration notes](../MIGRATION.md): what changed for existing installations.
