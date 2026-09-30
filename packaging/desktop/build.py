#!/usr/bin/env python3
"""Build BananaWiki Desktop with PyInstaller and package it for distribution.

Run on each target system (PyInstaller does not cross-compile)::

    python -m pip install . waitress pyinstaller
    python packaging/desktop/build.py

Output in ``packaging/desktop/dist/``:

* Windows: ``BananaWiki-Desktop-windows-<arch>.zip`` (``BananaWiki.exe``)
* Linux:   ``BananaWiki-Desktop-linux-<arch>.tar.gz`` (``BananaWiki``)
* macOS:   ``BananaWiki-Desktop-macos-<arch>.zip`` (``BananaWiki.app``)

The archives keep the executable permission bits, which plain artifact
uploads of a raw binary or ``.app`` folder would lose.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "bananawiki-desktop.spec"
DIST = HERE / "dist"
WORK = HERE / "build"
NAME = "BananaWiki"
MAC_APP = f"{NAME}.app"
ARCHIVE_PREFIX = f"{NAME}-Desktop"
EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def normalized_machine(machine: str) -> str:
    value = (machine or "unknown").strip().lower().replace(" ", "_")
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(value, value)


def built_artifact(system: str, dist: Path = DIST) -> Path:
    if system == "Darwin":
        return dist / MAC_APP
    if system == "Windows":
        return dist / f"{NAME}.exe"
    return dist / NAME


def _zip_tree(archive: zipfile.ZipFile, path: Path, name: str, executables: frozenset[str]) -> None:
    """Add *path* keeping Unix modes (and symlinks, which .app bundles use)."""
    info = zipfile.ZipInfo(name + ("/" if path.is_dir() and not path.is_symlink() else ""))
    info.create_system = 3
    mode = path.lstat().st_mode
    if path.is_symlink():
        info.external_attr = ((stat.S_IFLNK | 0o777) & 0xFFFF) << 16
        archive.writestr(info, os.readlink(path))
    elif path.is_dir():
        info.external_attr = (mode & 0xFFFF) << 16
        archive.writestr(info, "")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _zip_tree(archive, child, f"{name}/{child.name}", executables)
    else:
        if name in executables:
            mode |= EXECUTABLE_BITS
        info.external_attr = (mode & 0xFFFF) << 16
        archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def _zip_mac_app(app: Path, archive: Path, executable: str) -> None:
    ditto = shutil.which("ditto")
    if ditto:  # Finder-compatible, keeps extended attributes and code signatures
        subprocess.check_call([ditto, "-c", "-k", "--keepParent", app.name, str(archive)], cwd=str(app.parent))
        return
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        _zip_tree(out, app, app.name, frozenset({executable}))


def package_artifact(*, system: str | None = None, machine: str | None = None, dist: Path = DIST) -> Path:
    """Wrap the built app in the archive users download; returns its path."""
    system = system or platform.system()
    arch = normalized_machine(machine or platform.machine())
    artifact = built_artifact(system, dist)
    if not artifact.exists():
        raise FileNotFoundError(f"Build output not found: {artifact}")
    if system == "Darwin":
        inner = artifact / "Contents" / "MacOS" / NAME
        if not inner.is_file():
            raise FileNotFoundError(f"macOS executable not found: {inner}")
        inner.chmod(inner.stat().st_mode | EXECUTABLE_BITS)
        archive = dist / f"{ARCHIVE_PREFIX}-macos-{arch}.zip"
        archive.unlink(missing_ok=True)
        _zip_mac_app(artifact, archive, f"{MAC_APP}/Contents/MacOS/{NAME}")
        return archive
    if system == "Windows":
        archive = dist / f"{ARCHIVE_PREFIX}-windows-{arch}.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
            out.write(artifact, arcname=artifact.name)
        return archive
    artifact.chmod(artifact.stat().st_mode | EXECUTABLE_BITS)
    archive = dist / f"{ARCHIVE_PREFIX}-{system.lower()}-{arch}.tar.gz"
    archive.unlink(missing_ok=True)
    with tarfile.open(archive, "w:gz") as out:
        info = out.gettarinfo(artifact, arcname=artifact.name)
        info.mode |= EXECUTABLE_BITS
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        with artifact.open("rb") as handle:
            out.addfile(info, handle)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-build", action="store_true", help="only package an existing build")
    args = parser.parse_args(argv)
    if not args.skip_build:
        for module, package in (("PyInstaller", "pyinstaller"), ("waitress", "waitress")):
            if importlib.util.find_spec(module) is None:
                print(f"{module} is required: python -m pip install {package}", file=sys.stderr)
                return 2
        subprocess.check_call([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                               "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC)], cwd=str(HERE))
    print(package_artifact())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
