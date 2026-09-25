#!/usr/bin/env python3
"""Run a tenant web process and its durable TTS worker in one container."""

import os
import signal
import socket
import subprocess
import sys
import time


children = []
stopping = False


def _resolve_worker_tmp_dir():
    """Return a writable in-memory dir for Gunicorn heartbeats.

    Prefer ``/dev/shm`` (always RAM-backed), fall back to ``/tmp``
    (mounted as tmpfs inside tenant containers), and finally let
    Gunicorn pick its own default.
    """
    for candidate in ("/dev/shm", "/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return None


def _wait_for_web(port, timeout=120):
    """Wait for Gunicorn to finish preload and bind before TTS imports app."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stopping:
        if children and children[0].poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=.4):
                return True
        except OSError:
            time.sleep(.25)
    return False


def _stop(_signum=None, _frame=None):
    global stopping
    if stopping:
        return
    stopping = True
    for child in children:
        if child.poll() is None:
            child.terminate()


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    port = os.environ.get("BW_PORT", "5001")
    workers = os.environ.get("BW_INSTANCE_GUNICORN_WORKERS", "1")
    threads = os.environ.get("BW_INSTANCE_GUNICORN_THREADS", "4")
    gunicorn_cmd = [
        sys.executable, "-m", "gunicorn", "wsgi:app",
        "--bind", f"0.0.0.0:{port}",
        "--workers", workers,
        "--worker-class", "gthread",
        "--threads", threads,
        "--preload",
        "--timeout", "120",
        "--graceful-timeout", "10",
        "--max-requests", "5000",
        "--max-requests-jitter", "500",
        "--access-logfile", "/data/access.log",
        "--error-logfile", "/data/error.log",
    ]
    worker_tmp = _resolve_worker_tmp_dir()
    if worker_tmp:
        gunicorn_cmd += ["--worker-tmp-dir", worker_tmp]
    web = subprocess.Popen(gunicorn_cmd)
    children.append(web)
    startup_timeout = max(30, min(600, int(os.environ.get("BW_INSTANCE_STARTUP_TIMEOUT_SECONDS", "120"))))
    if not _wait_for_web(port, timeout=startup_timeout):
        print(f"Tenant web process did not become ready within {startup_timeout} seconds (exit code: {web.poll()}).", file=sys.stderr, flush=True)
        _stop()
        return web.returncode or 1
    tts = subprocess.Popen([
        sys.executable, "scripts/tts_worker.py", "--poll-interval",
        os.environ.get("BW_HOSTING_TTS_WORKER_POLL_INTERVAL_SECONDS", "30"),
    ])
    children.append(tts)
    while not stopping:
        if web.poll() is not None:
            _stop()
            break
        if tts.poll() is not None:
            # TTS is non-critical; restart it with a bounded delay.
            time.sleep(2)
            if not stopping:
                tts = subprocess.Popen([
                    sys.executable, "scripts/tts_worker.py", "--poll-interval",
                    os.environ.get("BW_HOSTING_TTS_WORKER_POLL_INTERVAL_SECONDS", "30"),
                ])
                children[-1] = tts
        time.sleep(.5)
    deadline = time.monotonic() + 12
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(.2)
    for child in children:
        if child.poll() is None:
            child.kill()
    return web.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
