"""``python -m bananawiki.desktop``: the launcher window, or headless commands.

::

    python -m bananawiki.desktop                          open the window
    python -m bananawiki.desktop serve --data-dir DIR [--port N] [--lan] [--language it]
    python -m bananawiki.desktop backup --data-dir DIR --output FILE.zip
    python -m bananawiki.desktop restore --data-dir DIR FILE.zip

``serve`` listens on this computer only unless ``--lan`` is given. The
packaged app accepts the same commands (the CI smoke test uses ``serve``).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from bananawiki.desktop import DesktopError, backup, i18n
from bananawiki.desktop.datafolder import DataFolder
from bananawiki.desktop.server import WikiServer, available_backends


def _say(text: str) -> None:
    # Windowed builds on Windows have no console: sys.stdout is None there.
    if sys.stdout is not None:
        print(text, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bananawiki-desktop", description="BananaWiki Desktop")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="serve a data folder without the window")
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--lan", action="store_true", help="let other devices on the local network connect")
    serve.add_argument("--language", choices=sorted(i18n.LANGUAGES), default=i18n.DEFAULT_LANGUAGE)
    serve.add_argument("--backend", choices=("waitress", "gunicorn"), default=None)
    save = commands.add_parser("backup", help="write a ZIP backup of a data folder")
    save.add_argument("--data-dir", required=True)
    save.add_argument("--output", required=True)
    load = commands.add_parser("restore", help="replace a data folder's data with a ZIP backup")
    load.add_argument("--data-dir", required=True)
    load.add_argument("archive")
    return parser


def _serve(args: argparse.Namespace) -> int:
    server = WikiServer(DataFolder(args.data_dir), share_on_lan=args.lan, port=args.port,
                        language=args.language, backend=args.backend)
    info = server.start()
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    _say(f"BananaWiki is running at {info.local_url} ({info.backend})")
    if info.lan_url:
        _say(f"Other devices on this network: {info.lan_url}")
    if not server.folder.setup_done():
        _say(f"Setup code for the first administrator: {info.setup_token}")
    try:
        while not stop.wait(1.0):
            if not server.running:
                _say("The server stopped unexpectedly; see the logs folder.")
                return 1
    finally:
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        from bananawiki.desktop.app import run

        return run()
    try:
        if args.command == "serve":
            if not available_backends():
                raise DesktopError("no_server_backend")
            return _serve(args)
        folder = DataFolder(args.data_dir)
        if args.command == "backup":
            _say(str(backup.create_backup(folder, args.output)))
        else:
            _say(f"Previous data kept in {backup.restore_backup(folder, args.archive)}")
    except DesktopError as error:
        if sys.stderr is not None:
            print(i18n.Translator(i18n.DEFAULT_LANGUAGE).error(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
