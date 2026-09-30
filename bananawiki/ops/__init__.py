# SPDX-FileCopyrightText: 2026 Luca Zani and BananaWiki contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""The ``banana`` lifecycle controller for managed BananaWiki servers.

A managed server lives under one root (``/opt/bananawiki`` by default)::

    config/            installation.json, source.json, updates.json, app.env,
                       repository credentials, journals and operation history
    repository.git     bare cache of the update source
    releases/<sha>/    one read-only tree per deployed revision, with its .venv
    current ->         symlink to the running release
    data/              everything the application writes
    site/              the static landing site (hosting mode)
    backups/ staging/  portable packages and scratch space

The on-disk layout and every ``config/*.json`` file (schema 1) are exactly the
ones BananaWiki 1.4 wrote, so the first update performed by the 1.4 updater
hands the server over to this controller without any conversion.

This package deliberately uses the standard library only: the installed
``/usr/local/bin/<service>`` wrapper runs it with the system Python, outside
the release's virtual environment.

BananaWiki 1.4 kept byte-identical copies of the lifecycle code in BananaChat
and enforced that with hash manifests in CI (``shared-files.json``). The
products now release independently, so that coupling is gone: this controller
is BananaWiki's own and the only compatibility promise is the on-disk format.
"""

SCHEMA = 1
PRODUCT = "BananaWiki"
MANAGED_MARKER = "# Managed by BananaSuite"
UNIT_GENERATION = "# bananawiki-ops: 2"
DEFAULT_ROOT = "/opt/bananawiki"
DEFAULT_SOURCE_URL = "https://github.com/OverloadedTech/BananaWiki.git"
