"""Tenant container entry point: the wiki web process plus its TTS worker.

Works under ``docker run --read-only --user <uid>:<gid> --cap-drop ALL`` with
``/data`` bind-mounted and tmpfs ``/tmp`` and ``/run``: nothing is written
outside ``/data`` and the tmpfs mounts. Logs go to stdout/stderr, where the
Docker log driver rotates them (1.4 appended to never-rotated files in /data).
``/health`` is served on ``BW_PORT`` (default 5001).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable


def web_command(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    return [
        sys.executable, "-m", "gunicorn", "-c", "python:bananawiki.ops.gunicorn_conf",
        "--workers", environ.get("BW_INSTANCE_GUNICORN_WORKERS", "1"),
        "--threads", environ.get("BW_INSTANCE_GUNICORN_THREADS", "4"),
        "--graceful-timeout", "10", "bananawiki.ops.wsgi:app",
    ]


def tts_command(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    return [sys.executable, "-m", "bananawiki.ops.tts_worker", "--poll-interval",
            environ.get("BW_HOSTING_TTS_WORKER_POLL_INTERVAL_SECONDS", "30")]


def wait_for_port(port: int, alive: Callable[[], bool], deadline: float) -> bool:
    while time.monotonic() < deadline and alive():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.25)
    return False


class Supervisor:
    def __init__(self, environ: dict[str, str] | os._Environ[str], spawn: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.environ = environ
        self.spawn = spawn
        self.children: list[subprocess.Popen] = []
        self.stopping = False

    def stop(self, *_: object) -> None:
        self.stopping = True
        for child in self.children:
            if child.poll() is None:
                child.terminate()

    def run(self) -> int:
        environment = {**self.environ, "PYTHONDONTWRITEBYTECODE": "1", "HOME": self.environ.get("HOME", "/tmp")}  # noqa: S108
        web = self.spawn(web_command(self.environ), env=environment)
        self.children.append(web)
        port = int(self.environ.get("BW_PORT", "5001"))
        timeout = max(30, min(600, int(self.environ.get("BW_INSTANCE_STARTUP_TIMEOUT_SECONDS", "120"))))
        if not wait_for_port(port, lambda: web.poll() is None and not self.stopping, time.monotonic() + timeout):
            print(f"The tenant web process did not become ready within {timeout} s.", file=sys.stderr, flush=True)
            self.stop()
            return web.wait() or 1
        tts = self.spawn(tts_command(self.environ), env=environment)
        self.children.append(tts)
        while not self.stopping and web.poll() is None:
            if tts.poll() is not None:
                time.sleep(2)  # TTS is optional: restart it with a delay, never take the wiki down
                if not self.stopping:
                    tts = self.spawn(tts_command(self.environ), env=environment)
                    self.children[-1] = tts
            time.sleep(0.5)
        self.stop()
        deadline = time.monotonic() + 12
        while any(child.poll() is None for child in self.children) and time.monotonic() < deadline:
            time.sleep(0.2)
        for child in self.children:
            if child.poll() is None:
                child.kill()
        return web.returncode or 0


def main() -> int:
    supervisor = Supervisor(os.environ)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
