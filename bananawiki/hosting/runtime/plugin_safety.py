"""Host-owned plugin safety state of one wiki (1.4 ``instance_manager`` plugin kill switch).

Kept under ``HOSTING_PLATFORM_STATE_DIR/<instance id>``, outside every tenant
mount, so nothing a tenant or its plugins do can change it:

* ``plugin_quarantine``: while this marker exists the wiki starts with
  external plugins switched off (``BW_ALLOW_EXTERNAL_PLUGINS=0``).
* ``plugin_snapshots/``: database copies with an ``index.json`` recording
  each file's label, time and SHA-256, so a restore never trusts a file name
  or an mtime. Labels: ``pre-enable`` (taken on the operator's request while
  the wiki is known to be good; the only kind a restore uses),
  ``operator-quarantine`` (forensics) and ``before-restore``. Five of each
  label are kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from . import RuntimeFailure

LABELS = frozenset({"pre-enable", "operator-quarantine", "before-restore"})
RESTORABLE = frozenset({"pre-enable"})
KEEP_PER_LABEL = 5
INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}_\d{6}Z-[a-z-]{1,32}\.db")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PluginState:
    def __init__(self, state_root: str, instance_id: str):
        if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
            raise RuntimeFailure("invalid", "invalid instance id")
        self.root = Path(state_root) / instance_id
        self.marker = self.root / "plugin_quarantine"
        self.snapshot_dir = self.root / "plugin_snapshots"
        self.index_path = self.snapshot_dir / "index.json"

    # Quarantine -------------------------------------------------------------

    def quarantined(self) -> bool:
        return self.marker.is_file() and not self.marker.is_symlink()

    def set_quarantine(self, active: bool) -> None:
        if not active:
            self.marker.unlink(missing_ok=True)
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.marker.write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
        os.chmod(self.marker, 0o600)

    # Snapshots --------------------------------------------------------------

    def _lock(self) -> FileLock:
        self.snapshot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return FileLock(str(self.snapshot_dir / ".index.lock"), timeout=30, mode=0o600)

    def _index(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {name: meta for name, meta in data.items() if isinstance(meta, dict) and SNAPSHOT_NAME.fullmatch(name)} \
            if isinstance(data, dict) else {}

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        descriptor, pending = tempfile.mkstemp(dir=self.snapshot_dir, prefix=".index-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(index, handle, indent=1, sort_keys=True)
            os.replace(pending, self.index_path)
        finally:
            Path(pending).unlink(missing_ok=True)

    def add(self, source: Path, label: str) -> str:
        """Move the database copy *source* into the snapshot store; returns its name."""
        if label not in LABELS:
            raise RuntimeFailure("invalid", "unknown snapshot label")
        now = datetime.now(UTC)
        name = f"{now.strftime('%Y%m%dT%H%M%S_%fZ')}-{label}.db"
        with self._lock():
            target = self.snapshot_dir / name
            shutil.move(str(source), target)
            os.chmod(target, 0o600)
            index = self._index()
            index[name] = {"label": label, "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                           "size": target.stat().st_size, "sha256": sha256(target)}
            same = sorted((meta["created_at"], item) for item, meta in index.items() if meta.get("label") == label)
            for _created, stale in same[:-KEEP_PER_LABEL]:
                index.pop(stale, None)
                (self.snapshot_dir / stale).unlink(missing_ok=True)
            self._write_index(index)
        return name

    def listing(self) -> list[dict[str, Any]]:
        items = []
        for name, meta in self._index().items():
            path = self.snapshot_dir / name
            if path.is_symlink() or not path.is_file():
                continue
            items.append({"name": name, "label": str(meta.get("label") or ""),
                          "created_at": str(meta.get("created_at") or ""), "size_bytes": int(meta.get("size") or 0),
                          "restorable": meta.get("label") in RESTORABLE})
        return sorted(items, key=lambda item: (item["created_at"], item["name"]), reverse=True)

    def restorable(self, name: str | None) -> tuple[str, Path]:
        """The newest (or the named) ``pre-enable`` snapshot, after checking its SHA-256."""
        index = self._index()
        candidates = [item for item in self.listing() if item["restorable"] and (name is None or item["name"] == name)]
        if not candidates:
            raise RuntimeFailure("not_found", "no restorable plugin safety snapshot")
        chosen = candidates[0]["name"]
        path = self.snapshot_dir / chosen
        if sha256(path) != index[chosen].get("sha256"):
            raise RuntimeFailure("db_unsafe", "the snapshot does not match its recorded checksum")
        return chosen, path
