"""Start and stop the wiki for one data folder.

Two ways to serve, both production WSGI servers:

* **waitress** (every platform, always bundled in the desktop builds): the
  wiki runs inside the launcher process on a background thread, so stopping
  is immediate and nothing is left behind.
* **gunicorn** (Linux and macOS, when waitress is not installed): the wiki
  runs as a child process with one worker.

The listen address is chosen by the caller: loopback unless ``share_on_lan``
is set, which the launcher only does after the user agreed to it. The data
folder stays locked while the server runs.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import DesktopError, backup, network
from .datafolder import DataFolder

log = logging.getLogger("bananawiki.desktop")

READY_TIMEOUT = 90.0
STOP_TIMEOUT = 15.0
THREADS = 8
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
_INHERITED_BLOCKLIST = ("SECRET_KEY", "GUNICORN_CMD_ARGS", "WSGI_APP")


@dataclass(frozen=True)
class ServerInfo:
    """Where a running wiki can be reached."""

    host: str
    port: int
    backend: str
    local_url: str
    lan_url: str | None
    setup_token: str

    @property
    def shared(self) -> bool:
        return self.host != network.LOOPBACK_HOST


class _Runner(Protocol):
    name: str

    def alive(self) -> bool: ...

    def stop(self) -> None: ...


def available_backends() -> list[str]:
    """Server backends usable here, preferred first."""
    found = []
    if importlib.util.find_spec("waitress") is not None:
        found.append("waitress")
    if os.name == "posix" and importlib.util.find_spec("gunicorn") is not None:
        found.append("gunicorn")
    return found


@contextlib.contextmanager
def scoped_environ(values: Mapping[str, str]) -> Iterator[None]:
    """Expose *values* in ``os.environ`` while the wiki application is built.

    The wiki reads a few folder variables from ``os.environ`` during start-up
    (for example to decide whether 1.4 folders need relocating); the previous
    values are restored afterwards.
    """
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _get(url: str, timeout: float) -> int:
    """GET *url* directly (never through a configured HTTP proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def wait_until_ready(port: int, runner: _Runner, timeout: float) -> None:
    """Poll ``/health`` until the wiki answers, the server dies or time runs out."""
    deadline = time.monotonic() + timeout
    health = network.url(network.LOOPBACK_HOST, port, "/health")
    delay = 0.05
    while time.monotonic() < deadline:
        if not runner.alive():
            raise DesktopError("server_exited")
        try:
            if _get(health, timeout=5) == 200:
                return
        except OSError:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 1.0)
    raise DesktopError("server_timeout")


# ── waitress (in process) ────────────────────────────────────────────────────


class _WaitressRunner:
    name = "waitress"

    def __init__(self, app: Any, host: str, candidates: list[int]):
        from waitress import create_server

        self.app = app
        last_error: OSError | None = None
        for port in candidates:
            try:
                self.server = create_server(app, host=host, port=port, threads=THREADS, ident="BananaWiki",
                                            clear_untrusted_proxy_headers=True)
            except OSError as error:
                if not network.is_address_in_use(error):
                    raise
                last_error = error
                continue
            self.port = int(self.server.effective_port)
            break
        else:
            raise DesktopError("no_free_port", str(last_error or ""))
        self.thread = threading.Thread(target=self._serve, name="bananawiki-desktop-server", daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            self.server.run()
        except Exception:  # noqa: BLE001 - reported through alive() and the log
            log.exception("The wiki server stopped unexpectedly")

    def alive(self) -> bool:
        return self.thread.is_alive()

    def _close_everything(self) -> None:
        # Runs on the server thread: closing the listener and every open
        # (keep-alive) connection empties the loop, which then returns.
        for dispatcher in list(self.server._map.values()):
            dispatcher.close()

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.trigger.pull_trigger(self._close_everything)
            self.thread.join(STOP_TIMEOUT)
        self.server.task_dispatcher.shutdown(timeout=STOP_TIMEOUT)
        scheduler = self.app.extensions.get("bananawiki.scheduler")
        if scheduler is not None:
            scheduler.stop()
            job_thread = getattr(scheduler, "_thread", None)
            if job_thread is not None:
                job_thread.join(STOP_TIMEOUT)


# ── gunicorn (child process) ─────────────────────────────────────────────────


class _GunicornRunner:
    name = "gunicorn"

    def __init__(self, folder: DataFolder, environ: dict[str, str], host: str, port: int):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("BW_") and key not in _INHERITED_BLOCKLIST}
        env.update(environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_PACKAGE_PARENT), env.get("PYTHONPATH", "")]))
        command = [
            sys.executable, "-m", "gunicorn",
            "--bind", f"{host}:{port}", "--workers", "1", "--threads", str(THREADS),
            "--graceful-timeout", "10", "--worker-tmp-dir", str(folder.root / "tmp_exports"),
            "bananawiki.wiki.app:create_app()",
        ]
        self.port = port
        self._log = (folder.logs / "server.log").open("ab")
        try:
            self.process = subprocess.Popen(command, cwd=str(folder.root), env=env, stdin=subprocess.DEVNULL,
                                            stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            self._log.close()
            raise

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(STOP_TIMEOUT)
        self._log.close()


# ── The server ───────────────────────────────────────────────────────────────


class WikiServer:
    """The wiki of one data folder, started and stopped on request."""

    def __init__(self, folder: DataFolder, *, share_on_lan: bool = False, port: int | None = None,
                 language: str = "en", backend: str | None = None):
        self.folder = folder
        self.share_on_lan = share_on_lan
        self.port = port
        self.language = language
        self.backend = backend
        self.info: ServerInfo | None = None
        self._runner: _Runner | None = None
        self._hold: contextlib.ExitStack | None = None
        self._state = threading.Lock()

    @property
    def running(self) -> bool:
        return self._runner is not None and self._runner.alive()

    def start(self, timeout: float = READY_TIMEOUT) -> ServerInfo:
        """Start serving and return once ``/health`` answers."""
        with self._state:
            if self._runner is not None:
                if self._runner.alive():
                    raise DesktopError("already_running")
                self._release()
            backends = available_backends()
            backend = self.backend or (backends[0] if backends else None)
            if backend is None or backend not in backends:
                raise DesktopError("no_server_backend", backend or "")
            hold = contextlib.ExitStack()
            try:
                hold.enter_context(self.folder.lock())
                backup.recover(self.folder)
                self.folder.prepare()
                runner, token = self._launch(backend)
                try:
                    wait_until_ready(runner.port, runner, timeout)  # type: ignore[attr-defined]
                except BaseException:
                    runner.stop()
                    raise
            except BaseException:
                hold.close()
                raise
            self._runner, self._hold = runner, hold
            self.info = self._describe(runner, token)
            log.info("Serving %s on %s with %s", self.folder.root, self.info.local_url, backend)
            return self.info

    def stop(self) -> None:
        """Stop serving and release the data folder. Does nothing when stopped."""
        with self._state:
            self._release()

    def _release(self) -> None:
        runner, hold = self._runner, self._hold
        self._runner = self._hold = None
        self.info = None
        try:
            if runner is not None:
                runner.stop()
        finally:
            if hold is not None:
                hold.close()

    def _host(self) -> str:
        return network.bind_host(self.share_on_lan)

    def _config(self, port: int):
        from ..wiki.config import load_config

        environ = self.folder.environ(host=self._host(), port=port, language=self.language)
        return environ, load_config(environ, secret_key=self.folder.secret_key())

    def _launch(self, backend: str) -> tuple[_Runner, str]:
        candidates = network.port_candidates(self.port)
        if backend == "waitress":
            from ..wiki.app import create_app

            environ, cfg = self._config(candidates[0])
            try:
                with scoped_environ(environ):
                    app = create_app(cfg)
            except Exception as error:
                log.exception("The wiki could not start")
                raise DesktopError("server_failed", str(error)) from error
            return _WaitressRunner(app, self._host(), candidates), cfg.setup_token
        port = next((p for p in candidates if network.port_available(self._host(), p)), None)
        if port is None:
            raise DesktopError("no_free_port")
        environ, cfg = self._config(port)
        return _GunicornRunner(self.folder, environ, self._host(), port), cfg.setup_token

    def _describe(self, runner: Any, token: str) -> ServerInfo:
        host, port = self._host(), int(runner.port)
        lan_ip = network.lan_address() if self.share_on_lan else None
        return ServerInfo(
            host=host,
            port=port,
            backend=runner.name,
            local_url=network.url(network.LOOPBACK_HOST, port),
            lan_url=network.url(lan_ip, port) if lan_ip else None,
            setup_token=token,
        )
