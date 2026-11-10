# Contributing to BananaWiki

For larger changes, please open an issue first so we can discuss the
approach. Small, focused fixes can go straight to a pull request. Report
security vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
and please follow the [code of conduct](CODE_OF_CONDUCT.md) in discussions and
reviews.

## Development setup

You need Python 3.11 or newer and Git.

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'   # pytest, pytest-xdist and the ruff version CI uses
bananawiki serve                  # http://127.0.0.1:5001, data in ./instance
bananawiki setup-token            # in a second terminal, for /setup
```

For read aloud, also install `.[tts]` (this needs ffmpeg too). For the hosting
portal's DNS and Google Drive helpers, install `.[hosting]`. The lifecycle and
backup tests need `git`, `age` and `ssh-keygen`
(`apt install age openssh-client`).

## Tests and linting

```sh
python -m pytest tests -q -n 2       # the whole suite, as CI runs it
python -m pytest tests/features/test_kanban*.py -q
python -m ruff check .
```

Run the tests with `python -m pytest` rather than `pytest`. The tests import
each other as a package, so the repository root has to be on the path.

`tests/conftest.py` documents the fixtures (`app`, `client`, `csrf_client`,
`db`, `make_user`, `login`, `admin`, `admin_client`, `app_factory`).
`tests/test_upgrade_from_1x.py` starts the current code on a real 1.4
instance. Please keep it passing, because existing installations upgrade in
place. CI runs ruff, the tests on Python 3.11 to 3.14, `pip-audit` and
ShellCheck, and it builds both container images and starts them read-only.

## How the code is organised

Before you write code, read [ARCHITECTURE.md](ARCHITECTURE.md). It covers the
package layout, the request pipeline, the `db` API, how features are declared,
events, template slots, interceptors, background jobs, the front-end rules and
the rules every change must follow. For a map of the repository, see
[docs/architecture.md](docs/architecture.md).

## Rules

Reviews check the rules at the end of ARCHITECTURE.md. In short:

* Every route checks authorisation explicitly, including object-level checks
  (this page, this board). Views are private by default.
* Never trust IDs sent by the client. Look up the object and check it.
* Don't use `|safe` on anything built from user input or on translated
  strings with parameters. Render Markdown with `markdown.render`.
* Save uploads with `storage.save` and send downloads with `storage.send`.
* Use POST (or PUT, PATCH and DELETE for JSON) for anything that changes
  state, never GET.
* Don't use inline event handlers. Pass data with
  `<script type="application/json">`, give any inline `<script>` a
  `nonce="{{ csp_nonce() }}"`, and insert user-supplied text into the DOM
  with `textContent`.
* Keep 1.4 URLs working, and add a redirect when a URL moves.
* Existing tables and columns keep their names and meaning. Schema changes go
  in a new numbered migration in `bananawiki/wiki/migrations/`. Never edit a
  released migration or `baseline_v3.sql`.
* Don't add dependencies without a strong reason, and avoid anything that
  needs a compiler at install time, because managed servers only install
  wheels.
* Every feature comes with tests for its permission checks, main flows and
  edge cases.
* Document new configuration in `docs/configuration.md`, and document
  behaviour changes that affect existing installations in `UPGRADING.md` and
  `CHANGELOG.md`.

## Translations

The interface is in English, Italian and German. Core strings live in
`bananawiki/wiki/translations/{en,it,de}.json`, each feature's in
`bananawiki/wiki/features/<id>/translations/{en,it,de}.json`, the portal's in
`bananawiki/hosting/translations/`, the desktop launcher's in
`bananawiki/desktop/translations/`. Every language is required and must have
the same keys, with the same placeholders (`{name}`) in each key.
`tests/test_core_translations.py` checks this. Keys under `js.` are sent to
the browser. Don't hard-code user-facing text in code or templates.

To add another interface language to a running wiki, upload a JSON file in
**Admin → Languages**. To ship a language with BananaWiki, add the files next
to the English ones (strings, `hosting/content/help.<code>.json` and
`wiki/features/auth/builtin_docs/<code>/`), list the code in the language
registries (`BUILTIN_LANGUAGES` in `bananawiki/wiki/i18n.py`, `LANGUAGES` in
`bananawiki/hosting/i18n.py`, `bananawiki/hosting/help.py`,
`bananawiki/desktop/i18n.py` and `bananawiki/wiki/features/auth/docs.py`),
document the portal's legal-page pair for the language
(`HOSTING_TERMS_URL_<CODE>` and `HOSTING_PRIVACY_URL_<CODE>`) in
`docs/configuration.md` and `.env.example` (`tests/test_env_example.py`
checks it) and open a pull request.

## Commits and pull requests

* Keep each pull request to one topic, and small enough to review.
* Write commit subjects in the imperative ("Fix category move check"). Add a
  body that explains why, if it isn't obvious.
* Describe what changed and how you tested it (commands and results), and
  include screenshots for visible changes.
* Don't commit runtime data, credentials, databases, generated builds or
  `instance/`.
* A maintainer reviews every change before it's merged.

## Licence

By contributing, you agree that your contributions are licensed under the
project's licence, the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). You keep the copyright to your contributions.
