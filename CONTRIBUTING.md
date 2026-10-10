# Contributing to BananaWiki

Thank you for helping. Open an issue to discuss a substantial change before
writing it, or send a pull request with a focused fix. Report security
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.11 or newer and Git.

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
bananawiki serve                  # http://127.0.0.1:5001, data in ./instance
bananawiki setup-token            # in a second terminal, for /setup
```

Optional: `.[tts]` for read aloud (plus ffmpeg), `.[hosting]` for the portal's
DNS and Google Drive helpers. The lifecycle and backup tests need `git`,
`age` and `ssh-keygen` (`apt install age openssh-client`).

## Tests and linting

```sh
python -m pytest tests/ -q -n 2      # the whole suite (pytest-xdist)
python -m pytest tests/features/test_kanban*.py -q
python -m ruff check .
```

`tests/conftest.py` documents the fixtures (`app`, `client`, `csrf_client`,
`db`, `make_user`, `login`, `admin`, `admin_client`, `app_factory`).
`tests/test_upgrade_from_1x.py` boots the current code on a real 1.4
instance; keep it green, because existing installations upgrade in place.
CI runs ruff, the tests on Python 3.11–3.13, `pip-audit`, ShellCheck, and
builds both container images and starts them read-only.

## How the code is organised

Read [ARCHITECTURE.md](ARCHITECTURE.md) before writing code: package layout,
request pipeline, the `db` API, how a feature is declared, events, template
slots, interceptors, background jobs, front-end rules and the review rules.
[docs/architecture.md](docs/architecture.md) maps the repository.

## Rules

The rules at the end of ARCHITECTURE.md are checked in review. In short:

* Every route checks authorisation explicitly, including object-level checks
  (this page, this board). Views are private by default.
* Never trust client-supplied ids; look the object up and check it.
* No `|safe` on anything built from user input or translated strings with
  parameters; Markdown goes through `markdown.render`.
* Uploads go through `storage.save`, downloads through `storage.send`.
* State changes use POST (or PUT/PATCH/DELETE for JSON), never GET.
* No inline scripts or event handlers (the CSP forbids them); build
  user-supplied text into the DOM with `textContent`.
* Keep 1.4 URLs working; redirect when a URL moves.
* Existing tables and columns keep their names and meaning; schema additions
  go into the feature's `schema.py` as idempotent `upgrade_v4(conn)` steps.
  Never change `baseline_v3.sql`.
* No new dependencies without a strong reason, and nothing that needs a
  compiler at install time (managed servers install wheels only).
* Every feature ships tests covering its permission checks, main flows and
  edge cases.
* Document new configuration in `docs/configuration.md` and behaviour changes
  that affect existing installations in `UPGRADING.md` and `CHANGELOG.md`.

## Translations

The interface is in English, Italian and German. Core strings live in
`bananawiki/wiki/translations/{en,it,de}.json`, each feature's in
`bananawiki/wiki/features/<id>/translations/{en,it,de}.json`, the portal's in
`bananawiki/hosting/translations/`, the desktop launcher's in
`bananawiki/desktop/translations/`. Every language is required and must have
the same keys, with the same placeholders (`{name}`) per key;
`tests/test_core_translations.py` checks this. Keys under `js.` are sent to
the browser. No hard-coded user-facing text in code or templates.

To add another interface language to a running wiki, upload a JSON file on
**Admin → Languages**; to ship it with BananaWiki, add the files next to the
English ones (strings, `hosting/content/help.<code>.json` and
`wiki/features/auth/builtin_docs/<code>/`), list the code in the language
registries (`BUILTIN_LANGUAGES` in `bananawiki/wiki/i18n.py`, `LANGUAGES` in
`bananawiki/hosting/i18n.py`, `bananawiki/hosting/help.py`,
`bananawiki/desktop/i18n.py` and `bananawiki/wiki/features/auth/docs.py`) and
open a pull request.

## Commits and pull requests

* One topic per pull request; keep it small enough to review.
* Write commit subjects in the imperative ("Fix category move check"), with a
  body that explains why when it is not obvious.
* Describe what changed, how you tested it (commands and results), and include
  screenshots for visible changes.
* Do not commit runtime data, credentials, databases, generated builds or
  `instance/`.
* Maintainers review every change; automated maintenance drafts are reviewed
  and approved by a person like any other contribution.

## License

Contributions are made under the project's license, the GNU Affero General
Public License version 3 (`AGPL-3.0-only`). Contributors keep the copyright
in their contributions.
