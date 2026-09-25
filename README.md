<img src="app/static/favicons/banana_yellow.png" alt="BananaWiki logo" width="64">

# BananaWiki

BananaWiki is a self-hosted wiki with page history, access controls, chat, kanban boards, canvas diagrams, and plugins. It can run as one wiki or as a hosting platform that manages separate wiki instances.

[Italiano](README.it.md) · [Deployment](docs/deployment.md) · [User guide](docs/user-guide/en/README.md) · [API](docs/api.md)

## Choose a deployment

| Use case | Run | Requirements |
| --- | --- | --- |
| One wiki for a team or community | The wiki application | Python 3.12 or newer |
| A service that provisions wikis for other people | The hosting portal and its tenant containers | Linux, Python 3.12 or newer, Docker, a reverse proxy |
| A local or classroom wiki | The portable desktop launcher | See the [portable app guide](docs/easy-deployment-app.md) |

The hosting platform includes approvals, account and instance suspension, an administrative decision history, storage limits, exports, and custom domains. Each instance's owner needs a platform administrator's permission before adding a custom domain.

## Run a single wiki locally

```sh
python3 -m venv .venv
. .venv/bin/activate
# a current pip first: older bundled releases lack its security fixes
python -m pip install --only-binary=:all: --no-deps --upgrade 'pip>=26.2.1'
python -m pip install -r requirements.txt
python -c 'import config; print(config.SETUP_TOKEN)'
gunicorn --bind 127.0.0.1:5001 --workers 2 --threads 4 wsgi:app
```

Open `http://127.0.0.1:5001` and use the printed installation token to create the first administrator. Keep that token private until setup is complete. Install FFmpeg if you use features that process audio or video.

These commands are for Linux and macOS. Reading pages aloud also needs the text-to-speech worker running next to the web server, `python scripts/tts_worker.py`; `banana install` and the desktop launcher start it for you.

For an internet-facing installation, follow the [deployment guide](docs/deployment.md). It covers HTTPS, persistent data, backups, and the hosting platform. Enable proxy trust only when the application's listening port is accessible through your trusted reverse proxy.

Managed servers use one command: `sudo ./banana install --mode wiki` or `--mode hosting`, then `sudo bananawiki update`, `backup`, `restore`, or `uninstall`. The command remembers the deployment mode. Automatic updates are off by default; `bananawiki updates enable` opts in and `updates disable` opts out. Choose your own Git URL, branch, and explicit fallback, including private repositories authenticated with a token or SSH deploy key.

[Encrypted repository backups](docs/backups.md) cover both single wikis and hosting platforms, including the deployed website. Choose a private GitHub or Forgejo destination, save the recovery key offline, and opt into scheduling if wanted. The hosting platform can also back up to Google Drive.

## Background

Luca Zani ([OverloadedTech](https://github.com/OverloadedTech)) started BananaWiki alone on 20 February 2026, when Canalescuola needed a wiki for the Officina Tecnologica project. Work sped up in June 2026 during an FSL placement (formazione scuola-lavoro, the Italian school work experience scheme), and since then Luca and Officina Tecnologica have maintained it. Over those months one wiki for one group turned into a platform that provisions and runs a wiki per tenant, with the approvals, quotas and custom domain handling that implies.

Until September 2026 it was an internal project. This release turns that internal version into free software: the same code runs the hosted service, which is open to the public at some times and not at others, and anyone can run it on their own server instead.

Two smaller projects grew alongside it. [BananaChat](https://github.com/BananaSuite/BananaChat) began as BananaAI, out of wanting to self-host AI models, and is now a separate way for people to try a local language model. [BananaVibe](https://github.com/BananaSuite/BananaVibe) is there to try out features quickly and keep up with routine maintenance, always ending at a draft pull request that a person reviews.

On the commit count: the code was developed privately for about seven months, and the internal history runs to a little over three thousand commits. None of it is published. It carries deployment credentials and notes about infrastructure written for maintainers only, and there is no way to remove every secret from that many commits with certainty. The public repository starts from a clean export of the current source instead. [NOTICE](NOTICE) keeps the original start date and the first commit hash on record.

## Documentation

[All of it is indexed in `docs/`](docs/README.md). The pages people reach for first:

- [Configuration](docs/configuration.md) and [operations](docs/operations.md)
- [Measured capacity and operating limits](docs/capacity.md)
- [Hosting platform](docs/hosting.md) and [custom domains](docs/custom-domains.md)
- [Migration from an existing installation](MIGRATION.md)
- [Plugin development](docs/plugins/overview.md)
- [Contributing](CONTRIBUTING.md), [code of conduct](CODE_OF_CONDUCT.md) and [security reports](SECURITY.md)

`site/` contains an editable static placeholder for a hosting service's home page. The bananawiki.com website is maintained in a separate private repository; the hosting portal's help pages ship here, in `hosting/content/`. Application updates preserve an operator's separately deployed website.

## License and ownership

BananaWiki is licensed under GNU AGPL version 3 (`AGPL-3.0-only`). Personal and commercial use are permitted. There are no software registration keys or commercial-use declarations.

If you modify the software and let people interact with it over a network, AGPL section 13 requires you to offer those users the corresponding source. Set `BW_SOURCE_URL` to the source of your deployed version; the application displays that link. See [LICENSE](LICENSE) for the full terms and retain third-party notices.

Copyright © 2026 Luca Zani and all contributors. Each contributor retains copyright in their contributions. The hosted BananaWiki service runs this same codebase.
