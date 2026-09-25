# Development and maintenance

Use Python 3.12 or newer and an isolated virtual environment. Install
`requirements.txt`, `pytest`, and `pytest-xdist`, then run
`python -m pytest -q -n 2`. Tests use fixture data; production credentials and
servers are unnecessary. The CI matrix checks Python 3.12 and 3.13.
Encrypted backup tests also need Git and age available on PATH.

The browser check covers desktop and mobile sidebar paging, retries, page
reordering and category edits with real sessions and CSRF protection. It creates
and removes a temporary local Wiki. Run it after changing navigation templates
or JavaScript:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python scripts/check_navigation_browser.py
```

Results and screenshots are written to `.browser-artifacts/`. CI runs the same
check with Chromium's system dependencies installed.

## What the root scripts are for

- `make help` lists the shortcuts. `make setup` creates `.venv` and installs
  the requirements; `make dev` and `make start` call the two scripts below;
  `make test` runs `pytest tests`. The `hosting-` variants do the same for the
  portal, with `hosting-test` limited to its own test files. `make source`
  writes `dist/BananaWiki-source.tar.gz` from the committed tree, and
  `make build-easy-deployment` builds the portable launcher.
- `./dev.sh` runs the Flask development server on port 5001. Development only;
  it prints a warning saying so.
- `./start.sh` runs Gunicorn with `gunicorn.conf.py`, and takes `--port` and
  `--workers` to override it.
- `python reset_password.py` lists the accounts and sets a new password for
  one of them. It is meant to be run over SSH by whoever administers the
  server, for the case where the only administrator is locked out.
- `python setup_wizard.py` is the older nginx and systemd deployment helper.
  See [deployment](deployment.md); prefer `banana install`.
- `./banana` is the managed installer. It is documented in
  [deployment](deployment.md) rather than here.

## Where to make changes

Page handlers are registered by `routes/wiki.py`. Its sibling modules separate
reading pages, editing, history, categories, reservations, contributions,
search, and editing presence. Shared access and protection checks live in
`wiki_common.py`. A page route keeps its original endpoint name when moved.

`app/templates/base.html` contains the document layout. The topbar, sidebar,
dialogs, customization panel, and client setup live under `templates/layout/`.
Page templates remain under their feature directories.

Browser code is grouped by feature under `app/static/js/`: navigation,
categories, announcements, customization, drafts, media, and editor tools.
The layout loads these as classic scripts in an explicit order; shared
functions remain available to page scripts. Keep that order when adding a
file. Use the translation catalogs for copy instead of repeating translated
text in template comments.

`routes/admin.py` registers the administration route groups. The handlers live
in `admin_accounts.py`, `admin_roles.py`, `admin_sessions.py`, `admin_settings.py`,
`admin_appearance.py`, `admin_localization.py`, `admin_migration.py`,
`admin_announcements.py`, `admin_badges.py`, `admin_checkouts.py`, and
`admin_contributions.py`. Shared validation and appearance helpers are in
`admin_common.py`. Keep existing endpoint names and authorization decorators
when moving a handler.

`hosting/routes/dashboard.py` registers the hosting dashboard groups. Its sibling
`dashboard_*.py` modules separate owner operations, administration, accounts,
feature requests, archives, collaboration, and settings. Shared access checks
and download helpers live in `dashboard_common.py`. The REST API in `api.py`
imports the same access checks, so a change to collaborator access applies to
the dashboard and the API alike.

`hosting/instance_manager.py` coordinates provisioning and lifecycle mutations.
The other `hosting/instance_*.py` modules own paths, environment construction,
database preparation, archive validation/export, and read-only diagnostics.
The manager re-exports the existing API for callers. Tests that mock an internal
dependency must patch the module that uses it.

## Shared deployment code

BananaWiki and BananaChat each include the lifecycle code, so either repository
can be built and installed on its own. The implementation, the launcher, the
maintenance tool and the lifecycle regressions are identical. Only
`banana_ops/product.py` identifies the application, and
`banana_ops/shared-files.json` records which files are common.

With both checkouts available locally, run this from the BananaWiki checkout:

```sh
python scripts/sync_lifecycle.py --check ../BananaChat
```

After reviewing a common change, record it and copy it to the other checkout:

```sh
python scripts/sync_lifecycle.py --record
python scripts/sync_lifecycle.py --write ../BananaChat
python scripts/sync_lifecycle.py --check ../BananaChat
```

The target path can have any directory name. The tool preserves the target's
product identity and unrelated files, rejects symbolic links, and refuses to
overwrite unrecorded common-file edits. Review the resulting Git diffs and run
the lifecycle tests in both repositories. Commit the manifest with the code.
Each repository's CI runs the local check; the cross-repository comparison
requires both checkouts. Updating a manifest records an intentional change,
so reviewers must still check that change.

CI also checks undefined Python names. Run the same check locally with
`ruff==0.16.6` installed:

```sh
python -m ruff check --select F821,F823 --exclude tests .
```
