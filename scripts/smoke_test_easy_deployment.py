#!/usr/bin/env python3
"""Smoke-test a packaged BananaWiki Easy Deployment artifact.

The test launches the packaged app in hidden ``--server-child`` mode against a
temporary portable data folder, waits for ``/healthz``, then stops it. This is
safe for CI because it does not open the GUI.
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
import urllib.request
import zipfile
from pathlib import Path


def _resolve_executable(path: Path) -> Path:
    path = path.resolve()
    if path.is_file():
        return path
    if path.suffix == ".app":
        exe = path / "Contents" / "MacOS" / "BananaWikiEasyDeployment"
        if exe.is_file():
            return exe
    candidates = [
        path / "BananaWikiEasyDeployment.exe",
        path / "BananaWikiEasyDeployment",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for candidate in path.glob("*.app"):
        exe = candidate / "Contents" / "MacOS" / "BananaWikiEasyDeployment"
        if exe.is_file():
            return exe
    raise FileNotFoundError(f"Could not find BananaWikiEasyDeployment executable under {path}")


def _is_within_directory(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _extract_zip(archive: Path, destination: Path) -> None:
    ditto = shutil.which("ditto")
    if ditto:
        subprocess.check_call([ditto, "-x", "-k", str(archive), str(destination)])
        return

    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = destination / member.filename
            if not _is_within_directory(destination, target):
                raise ValueError(f"Unsafe zip member path: {member.filename}")
            zf.extract(member, destination)
            mode = (member.external_attr >> 16) & 0o777
            if mode and target.exists():
                target.chmod(mode)


def _extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            target = destination / member.name
            if not _is_within_directory(destination, target):
                raise ValueError(f"Unsafe tar member path: {member.name}")
        tf.extractall(destination, filter="data")


def _extract_archive(artifact: Path) -> Path | None:
    name = artifact.name.lower()
    if not (name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz")):
        return None

    destination = Path(tempfile.mkdtemp(prefix="bananawiki-easy-artifact-"))
    try:
        if name.endswith(".zip"):
            _extract_zip(artifact, destination)
        else:
            _extract_tar(artifact, destination)
    except Exception:
        _best_effort_remove_tree(str(destination))
        raise
    return destination


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _best_effort_remove_tree(path: str) -> None:
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


def smoke_test(artifact: Path, timeout: float = 120.0) -> None:
    extracted = _extract_archive(artifact)
    artifact_root = extracted or artifact
    exe = _resolve_executable(artifact_root)
    port = _free_port()
    tmp = tempfile.mkdtemp(prefix="bananawiki-easy-smoke-")

    try:
        # Use a temporary file for logs to avoid pipe buffer deadlock
        log_file = tempfile.NamedTemporaryFile(delete=False, mode="w")
        log_path = log_file.name
        log_file.close()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = f"{str(exe.parent)}{os.pathsep}{env['PYTHONPATH']}"
        else:
            env["PYTHONPATH"] = str(exe.parent)

        try:
            # On Windows, avoid using shell=True to ensure we get the correct process handle
            # and a direct connection to stdout.
            log_f = open(log_path, "w")
            try:
                popen_kwargs = {
                    "stdout": log_f,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "env": env,
                    "cwd": str(exe.parent),
                }
                if os.name == "nt":
                    # CREATE_NO_WINDOW prevents the app from trying to open a console window
                    # which can sometimes hang in CI environments.
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

                proc = subprocess.Popen(
                    [
                        str(exe),
                        "--server-child",
                        "--data-dir",
                        tmp,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    **popen_kwargs,
                )
                try:
                    deadline = time.time() + timeout
                    last_error: Exception | None = None
                    while time.time() < deadline:
                        if proc.poll() is not None:
                            # Server exited early.
                            _stop_process_tree(proc)
                            log_f.close()

                            diagnostics = []

                            boot_diag = artifact_root / "_boot_diag.txt"
                            boot_diag_content = ""
                            if boot_diag.exists():
                                boot_diag_content = boot_diag.read_text(encoding='utf-8', errors='replace')
                            for fallback in [
                                Path.home() / "_boot_diag.txt",
                                Path(os.environ.get('TEMP', '')) / "_boot_diag.txt",
                                Path(os.environ.get('TMP', '')) / "_boot_diag.txt",
                            ]:
                                if fallback.exists() and not boot_diag_content:
                                    boot_diag_content = f"[found at {fallback}]\n" + fallback.read_text(encoding='utf-8', errors='replace')
                            if boot_diag_content:
                                diagnostics.append(f"Bootstrap diagnostic:\n{boot_diag_content}")


                            startup_marker = artifact_root / "_startup_marker.txt"
                            if startup_marker.exists():
                                diagnostics.append(f"Startup marker (exe dir):\n{startup_marker.read_text(encoding='utf-8', errors='replace')}")
                            startup_marker_data = Path(tmp) / "_startup_marker.txt"
                            if startup_marker_data.exists():
                                diagnostics.append(f"Startup marker (data dir):\n{startup_marker_data.read_text(encoding='utf-8', errors='replace')}")
                            elif not startup_marker.exists():
                                diagnostics.append("Startup marker: MISSING")

                            args_marker = Path(tmp) / "logs" / "_args_parsed.txt"
                            if args_marker.exists():
                                diagnostics.append(f"Args parsed marker:\n{args_marker.read_text(encoding='utf-8', errors='replace')}")

                            app_stdout_log = Path(tmp) / "logs" / "server-stdout.log"
                            if app_stdout_log.exists():
                                diagnostics.append(f"App server stdout:\n{app_stdout_log.read_text(encoding='utf-8', errors='replace')}")

                            deploy_log = Path(tmp) / "logs" / "easy-deployment.log"
                            if deploy_log.exists():
                                diagnostics.append(f"Deployment log:\n{deploy_log.read_text(encoding='utf-8', errors='replace')}")

                            with open(log_path, "r") as f:
                                proc_stdout = f.read()
                            if proc_stdout.strip():
                                diagnostics.append(f"Process stdout:\n{proc_stdout}")

                            diagnostics.append(f"Process exit code: {proc.returncode}")

                            combined = "\n\n".join(diagnostics) if diagnostics else "(no log output captured)"
                            raise RuntimeError(
                                f"Packaged server exited early with {proc.returncode}\n{combined[-5000:]}"
                            )
                        try:
                            # Try both 127.0.0.1 and localhost to bypass potential Windows binding issues
                            for target_host in ["127.0.0.1", "localhost"]:
                                try:
                                    with urllib.request.urlopen(f"http://{target_host}:{port}/healthz", timeout=2) as response:
                                        if response.status == 200:
                                            print(f"Packaged smoke test passed: {exe}")
                                            return
                                except Exception as e:
                                    last_error = e
                                    continue
                        except Exception as exc:
                            last_error = exc
                        time.sleep(0.5)

                    # Timeout reached
                    _stop_process_tree(proc)
                    log_f.close()

                    # Collect ALL diagnostic information
                    diagnostics = []

                    # 0. Bootstrap diagnostic (written by runtime hook IMMEDIATELY)
                    # Check multiple locations since sys.executable may differ
                    boot_diag = artifact_root / "_boot_diag.txt"
                    boot_diag_content = ""
                    if boot_diag.exists():
                        boot_diag_content = boot_diag.read_text(encoding='utf-8', errors='replace')
                    # Also check home dir and temp as fallbacks
                    for fallback in [
                        Path.home() / "_boot_diag.txt",
                        Path(os.environ.get('TEMP', '')) / "_boot_diag.txt",
                        Path(os.environ.get('TMP', '')) / "_boot_diag.txt",
                    ]:
                        if fallback.exists() and not boot_diag_content:
                            boot_diag_content = f"[found at {fallback}]\n" + fallback.read_text(encoding='utf-8', errors='replace')
                    if boot_diag_content:
                        diagnostics.append(f"Bootstrap diagnostic:\n{boot_diag_content}")


                    # 1. Check if main() was even reached (startup marker next to exe)
                    startup_marker = artifact_root / "_startup_marker.txt"
                    if startup_marker.exists():
                        diagnostics.append(f"Startup marker (exe dir):\n{startup_marker.read_text(encoding='utf-8', errors='replace')}")
                    startup_marker_data = Path(tmp) / "_startup_marker.txt"
                    if startup_marker_data.exists():
                        diagnostics.append(f"Startup marker (data dir):\n{startup_marker_data.read_text(encoding='utf-8', errors='replace')}")
                    elif not startup_marker.exists():
                        diagnostics.append("Startup marker: MISSING: process may be stuck in PyInstaller bootloader or runtime hook")

                    # 2. Check if args were parsed (marker in data dir)
                    args_marker = Path(tmp) / "logs" / "_args_parsed.txt"
                    if args_marker.exists():
                        diagnostics.append(f"Args parsed marker:\n{args_marker.read_text(encoding='utf-8', errors='replace')}")
                    else:
                        diagnostics.append("Args parsed marker: MISSING: main() likely never reached")

                    # 3. App's redirected stdout (written by run_server_child)
                    app_stdout_log = Path(tmp) / "logs" / "server-stdout.log"
                    if app_stdout_log.exists():
                        content = app_stdout_log.read_text(encoding="utf-8", errors="replace")
                        diagnostics.append(f"App server stdout:\n{content}" if content.strip() else "App server stdout: (file exists but empty)")
                    else:
                        diagnostics.append("App server stdout: MISSING: run_server_child() was never called")

                    # 4. Deployment log
                    deploy_log = Path(tmp) / "logs" / "easy-deployment.log"
                    if deploy_log.exists():
                        content = deploy_log.read_text(encoding="utf-8", errors="replace")
                        diagnostics.append(f"Deployment log:\n{content}" if content.strip() else "Deployment log: (file exists but empty)")

                    # 5. Subprocess stdout (empty on Windows windowed builds)
                    with open(log_path, "r") as f:
                        proc_stdout = f.read()
                    if proc_stdout.strip():
                        diagnostics.append(f"Process stdout:\n{proc_stdout}")

                    # 6. Process state after kill
                    rc = proc.poll()
                    diagnostics.append(f"Process exit code after kill: {rc}")

                    # 7. List all files in data dir for forensics
                    data_files = []
                    for p in sorted(Path(tmp).rglob("*")):
                        if p.is_file():
                            rel = p.relative_to(Path(tmp))
                            data_files.append(str(rel))
                    if data_files:
                        diagnostics.append("Files in data dir:\n" + "\n".join(data_files))

                    combined = "\n\n".join(diagnostics)
                    raise TimeoutError(
                        f"Timed out waiting for /healthz: {last_error}\n\n{combined[-15000:]}"
                    )
                finally:
                    # Ensure process is stopped and file is closed if we returned early or an exception occurred
                    if proc.poll() is None:
                        _stop_process_tree(proc)
                    try:
                        log_f.close()
                    except OSError:
                        pass
            except Exception:
                try:
                    log_f.close()
                except OSError:
                    pass
                raise
        finally:
            _best_effort_remove_tree(tmp)
            if extracted:
                _best_effort_remove_tree(str(extracted))
            if os.path.exists(log_path):
                os.remove(log_path)
    except Exception as exc:
        # Re-raise the exception to be caught by the main loop
        raise exc

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Packaged executable, .app, or onedir folder")
    args = parser.parse_args(argv)

    try:
        smoke_test(args.artifact)
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
