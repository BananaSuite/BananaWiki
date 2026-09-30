#!/usr/bin/env python3
"""Smoke-test a packaged BananaWiki Desktop build (used by CI on every OS).

Unpacks the archive, runs ``<app> serve`` on a free loopback port against a
temporary data folder, waits for ``/health`` and the setup page, stops it,
then makes a backup with ``<app> backup`` and checks the ZIP. No window is
opened, so it runs on headless CI machines.

    python packaging/desktop/smoke_test.py packaging/desktop/dist/BananaWiki-Desktop-linux-x86_64.tar.gz
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

NAME = "BananaWiki"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def unpack(artifact: Path, destination: Path) -> None:
    name = artifact.name.lower()
    if name.endswith(".zip"):
        ditto = shutil.which("ditto")
        if ditto:  # keeps the .app bundle's symlinks and modes
            subprocess.check_call([ditto, "-x", "-k", str(artifact), str(destination)])
            return
        with zipfile.ZipFile(artifact) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError(f"unsafe path in archive: {member.filename}")
                archive.extract(member, destination)
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(artifact) as archive:
            archive.extractall(destination, filter="data")
    else:
        raise ValueError(f"not a packaged artifact: {artifact}")


def find_executable(root: Path) -> Path:
    for candidate in (root / f"{NAME}.exe", root / NAME, root / f"{NAME}.app" / "Contents" / "MacOS" / NAME):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no {NAME} executable under {root}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def status(url: str) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":  # the one-file bootloader runs the app in a child process
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.terminate()
    try:
        process.wait(30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(10)


def diagnostics(data: Path, output: Path) -> str:
    parts = [f"--- process output ---\n{output.read_text(errors='replace')[-5000:]}"]
    log = data / "logs" / "bananawiki.log"
    if log.is_file():
        parts.append(f"--- {log.name} ---\n{log.read_text(errors='replace')[-5000:]}")
    return "\n".join(parts)


def serve_check(executable: Path, data: Path, scratch: Path, timeout: float) -> None:
    port = free_port()
    output = scratch / "serve.log"
    with output.open("wb") as log:
        process = subprocess.Popen([str(executable), "serve", "--data-dir", str(data), "--port", str(port)],
                                   stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   cwd=str(scratch), creationflags=NO_WINDOW)
    try:
        deadline = time.monotonic() + timeout
        while status(f"http://127.0.0.1:{port}/health") != 200:
            if process.poll() is not None:
                raise RuntimeError(f"the app exited with {process.returncode}\n{diagnostics(data, output)}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"no answer from /health\n{diagnostics(data, output)}")
            time.sleep(0.5)
        setup = status(f"http://127.0.0.1:{port}/setup")
        if setup != 200:
            raise RuntimeError(f"/setup answered {setup}\n{diagnostics(data, output)}")
    finally:
        stop(process)


def backup_check(executable: Path, data: Path, scratch: Path) -> None:
    target = scratch / "backup.zip"
    result = subprocess.run([str(executable), "backup", "--data-dir", str(data), "--output", str(target)],
                            capture_output=True, timeout=120, check=False, creationflags=NO_WINDOW)
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError(f"backup failed ({result.returncode}): {result.stdout!r} {result.stderr!r}")
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    if not {"bananawiki.db", "instance/.secret_key", "manifest.json"} <= names:
        raise RuntimeError(f"incomplete backup: {sorted(names)}")


def smoke_test(artifact: Path, timeout: float) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="bananawiki-desktop-smoke-"))
    try:
        unpack(artifact.resolve(), scratch / "app")
        executable = find_executable(scratch / "app")
        data = scratch / "data"
        serve_check(executable, data, scratch, timeout)
        backup_check(executable, data, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"Smoke test passed: {artifact.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        smoke_test(args.artifact, args.timeout)
    except Exception as error:  # noqa: BLE001 - report and fail the CI step
        print(f"Smoke test failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
