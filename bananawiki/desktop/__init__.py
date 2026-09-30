"""BananaWiki Desktop: run a local wiki from one data folder with one click.

The launcher is meant for teachers and other non-technical users. It serves
the ordinary wiki (:func:`bananawiki.wiki.app.create_app`) from a folder the
user chooses, on this computer only or, after explicit consent, on the local
network, and backs that folder up to a single ZIP file.

Modules:

* :mod:`.datafolder`  - the data folder layout, its lock and its secret key
* :mod:`.server`      - starting and stopping the wiki server
* :mod:`.backup`      - ZIP backups (online SQLite copy) and restores
* :mod:`.network`     - LAN address detection, ports and URLs
* :mod:`.preferences` - the launcher's remembered choices
* :mod:`.i18n`        - launcher strings (``translations/*.json``)
* :mod:`.app`         - the tkinter window
"""

from __future__ import annotations


class DesktopError(Exception):
    """A failure the launcher explains to the user.

    ``key`` names a string in the launcher translations (``error.<key>``);
    ``detail`` is shown after it, untranslated (a path or a system message).
    """

    def __init__(self, key: str, detail: str = ""):
        self.key = key
        self.detail = detail
        super().__init__(f"{key}: {detail}" if detail else key)
