# Contributing to BananaWiki

Open an issue to discuss a substantial change, or send a pull request with a focused fix. Describe the problem, the resulting behavior, and the checks you ran. Include screenshots when a visible change needs them. Do not include runtime data, credentials, or generated build output.

Use Python 3.12 or newer, create a virtual environment, and install `requirements.txt`. Install `pytest` to run the tests with `python -m pytest`. Tests should exercise behavior and regressions rather than reproduce implementation details.

Keep changes small enough to review. Preserve access checks, CSRF protection, resource limits, and third-party notices. Document new configuration and migration steps. Report security vulnerabilities using [SECURITY.md](SECURITY.md).

BananaVibe may prepare a maintenance draft from a maintainer-approved issue. Human maintainers still decide the design, review the diff, check behavior, and approve every merge. Do not include private prompts or discussion in public source or PR descriptions. Deployment changes must preserve opt-in updates, private credentials, and code/data recovery.

See [development and maintenance](docs/development.md) for the module layout and shared deployment workflow. Run `python scripts/sync_lifecycle.py --check` before committing; with both checkouts, add `../BananaChat` to compare them. Record reviewed common changes and copy them to the other checkout with the same script. Product identity stays in `banana_ops/product.py`.

Encrypted backup tests require Git and age (`apt install age` on Debian/Ubuntu); they use disposable local repositories and dummy credentials. Run `python scripts/sync_backups.py --check` for the backup code shared by all three applications. After reviewing a shared change, use `--record` and `--write ../OTHER_CHECKOUT`, review each diff, and commit the manifests with the implementation. The copy refuses unrecorded changes in the destination.

Contributions are made under the project's GNU AGPL version 3 license. Contributors retain copyright in their contributions.

Discussions and reviews follow the [code of conduct](CODE_OF_CONDUCT.md).
