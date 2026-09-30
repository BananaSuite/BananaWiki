"""Flat-key JSON translations with an English fallback.

A catalogue is a mapping ``language code -> {key: text}``. Keys are dotted
(``wiki.page.edit``); values use ``{name}`` placeholders. Bundled catalogues
are merged from several directories (core plus every feature). Languages that
administrators upload live in the instance directory and may override bundled
strings, which keeps them out of the source tree so updates never clobber them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger("bananawiki.i18n")

FALLBACK = "en"
META_KEY = "_meta"
LANG_CODE = re.compile(r"^[a-z]{2,12}(?:-[a-z0-9]{2,8})?$")
MAX_FILE_BYTES = 5 * 1024 * 1024
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def valid_code(code: object) -> str:
    text = str(code or "").strip().lower().replace("_", "-")
    return text if LANG_CODE.fullmatch(text) else ""


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_text(text: str, values: dict[str, Any]) -> str:
    if not values:
        return text
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


class Catalog:
    """Thread-safe, lazily loaded set of translations."""

    def __init__(self, bundled_dirs: Iterable[str | os.PathLike[str]], custom_dir: str | None = None):
        self.bundled_dirs = [Path(d) for d in bundled_dirs]
        self.custom_dir = Path(custom_dir) if custom_dir else None
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, str]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._custom_mtime: float = -1.0

    def add_directory(self, directory: str | os.PathLike[str]) -> None:
        path = Path(directory)
        if path.is_dir() and path not in self.bundled_dirs:
            self.bundled_dirs.append(path)
            self.reload()

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            self._meta.clear()

    def _custom_signature(self) -> float:
        if not self.custom_dir or not self.custom_dir.is_dir():
            return 0.0
        try:
            return max((p.stat().st_mtime for p in self.custom_dir.glob("*.json")), default=0.0)
        except OSError:
            return 0.0

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                log.warning("Ignoring oversized translation file %s", path)
                return {}
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            log.warning("Ignoring unreadable translation file %s: %s", path, error)
            return {}
        return data if isinstance(data, dict) else {}

    def _load(self, code: str) -> dict[str, str]:
        signature = self._custom_signature()
        if signature != self._custom_mtime:
            self._cache.clear()
            self._meta.clear()
            self._custom_mtime = signature
        cached = self._cache.get(code)
        if cached is not None:
            return cached
        merged: dict[str, str] = {}
        meta: dict[str, Any] = {}
        sources = [d / f"{code}.json" for d in self.bundled_dirs]
        if self.custom_dir:
            sources.append(self.custom_dir / f"{code}.json")
        for source in sources:
            data = self._read(source)
            if isinstance(data.get(META_KEY), dict):
                meta.update(data[META_KEY])
            merged.update({k: v for k, v in data.items() if k != META_KEY and isinstance(v, str)})
        self._cache[code] = merged
        self._meta[code] = meta
        return merged

    def strings(self, code: str) -> dict[str, str]:
        with self._lock:
            return self._load(valid_code(code) or FALLBACK)

    def meta(self, code: str) -> dict[str, Any]:
        with self._lock:
            code = valid_code(code) or FALLBACK
            self._load(code)
            return dict(self._meta.get(code, {}))

    def languages(self) -> list[str]:
        codes: set[str] = set()
        dirs = list(self.bundled_dirs)
        if self.custom_dir:
            dirs.append(self.custom_dir)
        for directory in dirs:
            if directory.is_dir():
                codes.update(valid_code(p.stem) for p in directory.glob("*.json"))
        codes.discard("")
        return sorted(codes)

    def translate(self, code: str, key: str, default: str | None = None, /, **values: Any) -> str:
        text = self.strings(code).get(key)
        if text is None and code != FALLBACK:
            text = self.strings(FALLBACK).get(key)
        if text is None:
            text = default if default is not None else key
        return format_text(text, values)

    def write_custom(self, code: str, data: dict[str, Any]) -> None:
        """Persist an administrator-supplied language file atomically."""
        code = valid_code(code)
        if not code or not self.custom_dir:
            raise ValueError("Invalid language code")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        target = self.custom_dir / f"{code}.json"
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
        self.reload()

    def delete_custom(self, code: str) -> bool:
        code = valid_code(code)
        if not code or not self.custom_dir:
            return False
        target = self.custom_dir / f"{code}.json"
        if target.is_file():
            target.unlink()
            self.reload()
            return True
        return False
