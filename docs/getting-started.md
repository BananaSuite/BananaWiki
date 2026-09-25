# Getting started

BananaWiki runs as a single wiki or as a hosting platform that creates separate
wiki containers. Start with a single wiki unless you need to provision wikis for
other people.

## Run a wiki locally

Install Python 3.12 or newer, then run:

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c 'import config; print(config.SETUP_TOKEN)'
.venv/bin/gunicorn --bind 127.0.0.1:5001 --workers 2 --threads 4 wsgi:app
```

Open `http://127.0.0.1:5001`. Enter the installation token printed above to create
the first administrator and choose the wiki's name and timezone. Later changes
are available in **Admin → Settings**. Install FFmpeg for audio and video features.

The token protects first-run setup. It is unrelated to software licensing:
BananaWiki is AGPL-licensed and permits personal and commercial use.

## Configure your wiki

Create categories and pages, choose which plugins to enable, then invite users.
Use **Admin → Users** and [permissions](permissions.md) to set their access.
Keep open signup disabled unless you intend to accept public registrations.

Environment settings in `config.py` are loaded at startup. Settings stored by the
administrator interface cover appearance, user access, enabled features, and
backups. See [configuration](configuration.md) for their defaults and limits.

## Deploy a service

Follow [deployment](deployment.md) for persistent data, HTTPS, process supervision,
and backups. The guide has separate instructions for a single wiki and the
hosting platform. The portal requires Linux and Docker; a single wiki does not.

The application does not edit a public website. `site/index.html` is a static
placeholder that operators can replace and deploy separately. The bananawiki.com
website is kept in a separate private repository; the portal's help centre
ships here, in `hosting/content/`.

For an existing installation, start with [migration](../MIGRATION.md). Preserve
data and session keys before changing the source checkout or deployment layout.

## Development

[Contributing](../CONTRIBUTING.md) explains test dependencies and commands.
The [user guide](user-guide/en/README.md), [plugin guide](plugins/overview.md),
and [API reference](api.md) cover application features and integrations.
