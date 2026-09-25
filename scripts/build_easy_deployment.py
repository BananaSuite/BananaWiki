#!/usr/bin/env python3
"""Build and package the portable BananaWiki launcher with PyInstaller."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "easy_deployment_app"
ORIGINAL_SPEC = APP_DIR / "bananawiki_easy_deployment.spec"
DEFAULT_SPEC = ORIGINAL_SPEC
DIST_DIR = APP_DIR / "dist"
EXE_NAME = "BananaWikiEasyDeployment"
MAC_APP_NAME = "BananaWiki Easy Deployment.app"
MAIN_PY = APP_DIR / "main.py"


def _normalized_machine(machine: str) -> str:
    value = (machine or "unknown").strip().lower().replace(" ", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(value, value)


def _built_artifact_path(system: str, dist_dir: Path | None = None) -> Path:
    dist = dist_dir or DIST_DIR
    if system == "Darwin":
        return dist / MAC_APP_NAME
    if system == "Windows":
        return dist / f"{EXE_NAME}.exe"
    return dist / EXE_NAME


def _ensure_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_zip_entry(
    zf: zipfile.ZipFile,
    path: Path,
    arcname: str,
    *,
    executable_relpaths: frozenset[str] | None = None,
) -> None:
    st = path.lstat()
    if path.is_symlink():
        info = zipfile.ZipInfo(arcname)
        info.create_system = 3
        info.external_attr = ((stat.S_IFLNK | 0o777) & 0xFFFF) << 16
        zf.writestr(info, os.readlink(path))
        return

    if path.is_dir():
        info = zipfile.ZipInfo(f"{arcname}/")
        info.create_system = 3
        info.external_attr = (st.st_mode & 0xFFFF) << 16
        zf.writestr(info, "")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _write_zip_entry(
                zf,
                child,
                f"{arcname}/{child.name}",
                executable_relpaths=executable_relpaths,
            )
        return

    mode = st.st_mode
    if executable_relpaths and arcname in executable_relpaths:
        mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

    info = zipfile.ZipInfo(arcname)
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    with path.open("rb") as fh:
        zf.writestr(info, fh.read(), compress_type=zipfile.ZIP_DEFLATED)


def _zip_macos_app(
    app_path: Path,
    archive_path: Path,
    *,
    executable_relpaths: frozenset[str] | None = None,
) -> None:
    """Create a Finder-friendly zip while preserving .app executable bits."""
    ditto = shutil.which("ditto")
    if ditto:
        subprocess.check_call(
            [ditto, "-c", "-k", "--keepParent", app_path.name, str(archive_path)],
            cwd=str(app_path.parent),
        )
        return

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_zip_entry(
            zf, app_path, app_path.name, executable_relpaths=executable_relpaths
        )


def package_portable_artifact(
    *,
    system: str | None = None,
    machine: str | None = None,
    dist_dir: Path | None = None,
) -> Path:
    """Return the distributable artifact path, packaging Unix targets first."""
    system = system or platform.system()
    machine = machine or platform.machine()
    dist = dist_dir or DIST_DIR
    artifact = _built_artifact_path(system, dist)
    if not artifact.exists():
        raise FileNotFoundError(f"Expected build artifact not found: {artifact}")

    arch = _normalized_machine(machine)
    if system == "Darwin":
        app_exe = artifact / "Contents" / "MacOS" / EXE_NAME
        if not app_exe.is_file():
            raise FileNotFoundError(f"Expected macOS app executable not found: {app_exe}")
        _ensure_executable(app_exe)
        archive = dist / f"{EXE_NAME}-macos-{arch}.zip"
        archive.unlink(missing_ok=True)
        exe_relpath = f"{MAC_APP_NAME}/Contents/MacOS/{EXE_NAME}"
        _zip_macos_app(
            artifact,
            archive,
            executable_relpaths=frozenset({exe_relpath}),
        )
        return archive

    if system == "Linux":
        _ensure_executable(artifact)
        archive = dist / f"{EXE_NAME}-linux-{arch}.tar.gz"
        archive.unlink(missing_ok=True)
        with tarfile.open(archive, "w:gz") as tf:
            info = tf.gettarinfo(artifact, arcname=artifact.name)
            info.mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            with open(artifact, "rb") as fh:
                tf.addfile(info, fh)
        return archive

    if system == "Windows":
        archive = dist / f"{EXE_NAME}-windows-{arch}.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(artifact, arcname=artifact.name)
        return archive

    return artifact




def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build BananaWiki Easy Deployment App",
    )
    parser.parse_args()

    system = platform.system()
    machine = platform.machine()

    spec = DEFAULT_SPEC
    print(f"Building BananaWiki Easy Deployment for {system} {machine}")

    if not spec.exists():
        print(f"Spec file not found: {spec}")
        return 1

    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("PyInstaller is required. Install it with:")
        print("  python -m pip install pyinstaller")
        return 2

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("BW_ENV", "development")
    env.setdefault("SECRET_KEY", "bananawiki-easy-deployment-build-secret")

    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(spec),
        ],
        cwd=str(APP_DIR),
        env=env,
    )

    print()
    print("Build complete.")
    print(f"Output directory: {DIST_DIR}")
    package = package_portable_artifact(system=system, machine=machine)
    print(f"Portable artifact: {package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
