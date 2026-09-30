# BananaWiki documentation

Documentation for BananaWiki 1.6. The [project README](../README.md) is the
short introduction; upgrading from 1.4 is in [UPGRADING](../UPGRADING.md).

## Install and run

* [Getting started](getting-started.md): a wiki on your computer and the first
  sign-in.
* [Deployment](deployment.md): managed server with `banana`, Docker Compose,
  manual Gunicorn and Caddy, the hosting platform.
* [Configuration](configuration.md): every environment variable with its
  default.
* [Operations](operations.md): updates, backups, restore and rollback,
  background jobs, logs, health checks, and the `banana` and `bananawiki`
  command references.
* [Desktop](desktop.md): BananaWiki Desktop for Windows, macOS and Linux.

## Understand and secure it

* [Security](security.md): trust model, sessions, CSRF, CSP, uploads,
  secrets, checklist.
* [Roles and permissions](permissions.md): roles, custom roles, category
  access and the permission catalogue.
* [Features](features.md): every feature, its switch, settings and
  permissions.
* [Architecture](architecture.md): a map of the repository, pointing to
  [ARCHITECTURE.md](../ARCHITECTURE.md).
* [1.4 review](1.4-review.md): everything the audits found wrong in 1.4 and
  what 1.6 does about each item.

## Extend and integrate

* [REST API](api.md): tokens, scopes, endpoints, errors, userbots.
* [Federation](federation.md): pairing wikis and sharing pages.
* [Plugins](plugins/README.md): writing, packaging and installing plugins;
  1.4 plugins.
* [Read aloud](tts.md): Piper voices, the worker and the GPU speech server.

## Hosting platform

* [Hosting](hosting.md): portal, runtime agent, isolation, custom domains,
  accounts, wikis, backups, portal API.

## Using the wiki

* [User guide](user-guide/en/README.md) (English).
* [Guida utente](user-guide/it/README.md) (italiano).

## Project

* [Contributing](../CONTRIBUTING.md), [security reports](../SECURITY.md),
  [changelog](../CHANGELOG.md), [code of conduct](../CODE_OF_CONDUCT.md).
