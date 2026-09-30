"""What the launcher remembers between runs.

Stored as JSON in the user's configuration folder (never in the data folder,
which may be on a USB stick or shared): the last data folder, the launcher
language, whether network sharing was chosen and a preferred port.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import i18n
from .datafolder import write_private

FILE_NAME = "desktop.json"
LEGACY_FOLDER_NAME = "bananawiki"  # the 1.4 launcher's folder beside the executable


@dataclass
class Preferences:
    data_dir: str = ""
    language: str = ""
    share_on_lan: bool = False
    port: int | None = None


def config_dir() -> Path:
    """The per-user configuration folder for BananaWiki Desktop."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "BananaWiki"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "BananaWiki"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bananawiki"


def executable_dir() -> Path | None:
    """The folder that holds the packaged app (beside the ``.app`` bundle on macOS)."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent.parent
    return executable.parent


def default_data_dir() -> Path:
    """Where a first run keeps its data.

    A packaged app keeps it beside itself when it can (portable use, and where
    1.4 kept it); otherwise it goes to the user's Documents folder.
    """
    beside = executable_dir()
    if beside is not None:
        legacy = beside / LEGACY_FOLDER_NAME
        if legacy.is_dir() or os.access(beside, os.W_OK):
            return legacy
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "BananaWiki"


def _clean(raw: object) -> Preferences:
    prefs = Preferences()
    if not isinstance(raw, dict):
        return prefs
    if isinstance(raw.get("data_dir"), str):
        prefs.data_dir = raw["data_dir"]
    if raw.get("language") in i18n.LANGUAGES:
        prefs.language = raw["language"]
    prefs.share_on_lan = raw.get("share_on_lan") is True
    port = raw.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        prefs.port = port
    return prefs


def load(path: Path | None = None) -> Preferences:
    """The saved preferences, with defaults for anything missing or invalid."""
    path = path or config_dir() / FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    prefs = _clean(raw)
    prefs.data_dir = prefs.data_dir or str(default_data_dir())
    prefs.language = prefs.language or i18n.system_language()
    return prefs


def save(prefs: Preferences, path: Path | None = None) -> None:
    path = path or config_dir() / FILE_NAME
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, json.dumps(asdict(prefs), indent=2).encode("utf-8"))
