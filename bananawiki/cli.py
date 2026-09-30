"""``bananawiki``: application administration from the command line.

Wiki commands (configuration comes from ``BW_*`` variables, as for the server)::

    bananawiki serve [--host H] [--port P] [--debug]   development server
    bananawiki setup-token                            the first-run setup token
    bananawiki migrate | db migrate                   create or upgrade the database
    bananawiki db check | db backup [--output FILE]   integrity check, online copy
    bananawiki db prune-retired [--yes]               drop tables of retired 1.4 plugins
    bananawiki create-admin NAME [--owner] [--password-file F]
    bananawiki reset-password NAME [--password-file F | --generate] [--temporary]
    bananawiki jobs list | jobs run [NAME]            background jobs (e.g. from cron)
    bananawiki config check                           validate the environment
    bananawiki export --output FILE | import FILE     move a wiki's instance directory

Server lifecycle commands (``install``, ``update``, ``backup``, ``restore``,
``rollback``, ``status``, …) and any invocation with ``--root`` are handed to
:mod:`bananawiki.ops.cli`, so ``bananawiki update`` means the same here as
through the installed wrapper. Passwords are never accepted on the command
line: they are prompted for or read from a file.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIFECYCLE_COMMANDS = frozenset({
    "install", "update", "backup", "restore", "rollback", "recover", "status", "start", "stop", "restart",
    "uninstall", "updates", "source", "proxy", "backups", "agent",
})
RETIRED_PREFIXES = ("banana_ai__", "meetings__", "bw_oauth_provider__", "oauth_login__", "git_override__", "feedback__")
RETIRED_TABLES = frozenset({"beta_testers", "beta_tester_invites", "feedback_reports", "feedback_banned_users",
                            "api_tokens", "userbot_api_tokens"})
EXPORT_FORMAT = "bananawiki-instance-export"


class CommandError(Exception):
    """A user-facing failure: printed without a traceback, exit status 1."""


# Helpers ---------------------------------------------------------------------


def _config(**overrides: Any):
    from .wiki.config import load_config

    return load_config(**overrides)


@contextmanager
def _app_scope() -> Iterator[Any]:
    """The application with a database connection bound to this thread."""
    from .wiki.app import create_app
    from .wiki.db import connection_scope

    app = create_app(_config(run_background_jobs=False))
    with app.app_context(), connection_scope():
        yield app


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _read_password(args: argparse.Namespace, *, prompt: str) -> str:
    if getattr(args, "password_file", None):
        path = Path(args.password_file)
        if not path.is_file():
            raise CommandError(f"{path} is not a file.")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat: ") != first:
        raise CommandError("The passwords do not match.")
    return first


def _account_error(error: Exception) -> CommandError:
    from .wiki.i18n import t

    key = getattr(error, "key", str(error))
    try:
        return CommandError(t(key, **getattr(error, "values", {})))
    except Exception:  # noqa: BLE001 - fall back to the key when translations are unavailable
        return CommandError(key)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


# Commands --------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    host = args.host or "127.0.0.1"
    if args.debug and not _is_loopback(host):
        raise CommandError("The debugger executes code for anyone who can reach it: --debug needs a loopback host.")
    from .wiki.app import create_app

    os.environ.setdefault("BW_ENV", "development")
    app = create_app(_config())
    if not _is_loopback(host):
        print(f"Warning: serving on {host} without TLS; use Gunicorn behind a proxy in production.", file=sys.stderr)
    app.run(host=host, port=args.port, debug=args.debug, use_reloader=args.debug)
    return 0


def cmd_setup_token(args: argparse.Namespace) -> int:
    from .wiki import settings

    with _app_scope() as app:
        if settings.setup_done():
            print("Setup is already complete; the token is no longer accepted.", file=sys.stderr)
            return 1
        print(app.config["BW"].setup_token)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from .wiki import takeover
    from .wiki.app import open_database

    cfg = _config()
    database = open_database(cfg)
    applied = database.initialize(before=lambda conn: takeover.backup_before_upgrade(cfg, database, conn))
    _app_id, version = database.version()
    _print({"database": str(database.path), "migrations_applied": applied, "schema_version": version})
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    if args.db_action == "migrate":
        return cmd_migrate(args)
    from .wiki.app import open_database

    cfg = _config()
    database = open_database(cfg)
    if args.db_action == "check":
        result = database.integrity_check()
        app_id, version = database.version()
        _print({"database": str(database.path), "integrity": result, "application_id": hex(app_id),
                "schema_version": version, "supported_version": database.latest_version})
        return 0 if result == "ok" and version <= database.latest_version else 1
    if args.db_action == "backup":
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        output = Path(args.output) if args.output else Path(cfg.instance_dir) / "backups" / f"manual-{stamp}.db"
        if output.exists():
            raise CommandError(f"{output} exists; choose a new file name.")
        _print({"backup": str(database.backup_to(output))})
        return 0
    return prune_retired(database, assume_yes=args.yes)


def retired_tables(conn: sqlite3.Connection) -> list[str]:
    from .core.sqlite import tuples

    names = [row[0] for row in tuples(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    return sorted(name for name in names if name in RETIRED_TABLES or name.startswith(RETIRED_PREFIXES))


def prune_retired(database: Any, *, assume_yes: bool) -> int:
    """Drop the tables of plugins that 1.6 no longer ships, after an online backup."""
    from .core.sqlite import quote_identifier, tuples

    conn = database.connect()
    try:
        tables = retired_tables(conn)
        if not tables:
            _print({"dropped": []})
            return 0
        print("Tables of retired 1.4 plugins:\n  " + "\n  ".join(tables), file=sys.stderr)
        if not assume_yes:
            if not sys.stdin.isatty():
                raise CommandError("Confirm with --yes when not running interactively.")
            if input("Type 'drop' to delete them permanently: ").strip() != "drop":
                print("Nothing was changed.", file=sys.stderr)
                return 1
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = database.backup_to(database.path.parent / "backups" / f"pre-prune-{stamp}.db")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
            if tuples(conn, "PRAGMA foreign_key_check"):
                raise CommandError("Dropping these tables would break foreign keys; nothing was changed.")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()
    _print({"dropped": tables, "backup": str(backup)})
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from .wiki import accounts, settings

    password = _read_password(args, prompt=f"Password for {args.username}: ")
    with _app_scope():
        try:
            user = accounts.create(args.username, password, role="owner" if args.owner else "admin")
        except accounts.AccountError as error:
            raise _account_error(error) from None
        if not settings.setup_done():
            settings.update({"setup_done": 1}, internal=True)
    _print({"created": user["username"], "role": user["role"], "id": user["id"]})
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    from .wiki import accounts

    password = secrets.token_urlsafe(12) if args.generate else _read_password(
        args, prompt=f"New password for {args.username}: ")
    with _app_scope():
        user = accounts.by_username(args.username)
        if user is None:
            raise CommandError(f"No account named {args.username!r}.")
        try:
            accounts.set_password(user["id"], password, require_change=args.temporary or args.generate)
        except accounts.AccountError as error:
            raise _account_error(error) from None
    result: dict[str, Any] = {"reset": user["username"], "sessions_revoked": True,
                              "must_change": bool(args.temporary or args.generate)}
    if args.generate:
        result["password"] = password
    _print(result)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    with _app_scope() as app:
        scheduler = app.extensions["bananawiki.scheduler"]
        if args.jobs_action == "list":
            _print([{"feature": feature.id, "job": job.name, "interval_seconds": job.interval}
                    for feature, job in scheduler.jobs()])
            return 0
        known = {job.name for _feature, job in scheduler.jobs()}
        if args.name and args.name not in known:
            raise CommandError(f"Unknown job {args.name!r}. Use 'jobs list'.")
    ran = scheduler.run_due(force=True, only=args.name)
    _print({"ran": ran})
    return 0


def config_warnings(cfg: Any) -> list[str]:
    warnings = []
    if cfg.is_production and not cfg.proxy_mode and not _is_loopback(cfg.host):
        warnings.append("Production without BW_PROXY_MODE: serve through an HTTPS proxy.")
    if cfg.is_production and not cfg.preferred_url_scheme and not cfg.proxy_mode:
        warnings.append("BW_PREFERRED_URL_SCHEME is unset; links in e-mails may use http://.")
    for name, folder in vars(cfg.folders).items():
        parent = Path(folder)
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            warnings.append(f"The {name} folder {folder} is not writable.")
    return warnings


def cmd_config(args: argparse.Namespace) -> int:
    from .core.env import ConfigError

    try:
        cfg = _config()
    except ConfigError as error:
        raise CommandError(f"Invalid configuration: {error}") from None
    warnings = config_warnings(cfg)
    _print({"environment": cfg.env, "instance_dir": cfg.instance_dir, "database": cfg.database_path,
            "listen": f"{cfg.host}:{cfg.port}", "proxy_mode": cfg.proxy_mode, "warnings": warnings})
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """A tarball of the instance directory with a consistent database copy."""
    from .ops.files import regular_files, write_package
    from .wiki.app import open_database

    cfg = _config()
    instance = Path(cfg.instance_dir)
    database = open_database(cfg)
    output = Path(args.output).absolute()
    if output.is_relative_to(instance):
        raise CommandError("Write the export outside the instance directory.")
    with tempfile.TemporaryDirectory(prefix="export-") as scratch:
        copy = database.backup_to(Path(scratch) / "bananawiki.db")
        skip = {database.path.name + suffix for suffix in ("", "-wal", "-shm", "-journal", ".initialized")}
        inputs = [(path, "instance/" + name) for path, name in regular_files(instance)
                  if name not in skip and not name.startswith("backups/")]
        inputs.append((copy, "database/bananawiki.db"))
        write_package(output, {"product": EXPORT_FORMAT, "created_at": datetime.now(UTC).isoformat()}, inputs)
    _print({"export": str(output)})
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .ops.files import read_package

    cfg = _config()
    instance = Path(cfg.instance_dir)
    if instance.exists() and any(instance.iterdir()):
        raise CommandError(f"{instance} is not empty; import only into a new instance directory.")
    instance.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="import-") as scratch:
        try:
            with read_package(Path(args.package), EXPORT_FORMAT, Path(scratch)) as (tree, _manifest):
                if (tree / "instance").is_dir():
                    shutil.copytree(tree / "instance", instance, dirs_exist_ok=True)
                target = Path(cfg.database_path)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(tree / "database/bananawiki.db", target)
                target.chmod(0o600)
        except (ValueError, tarfile.TarError) as error:
            raise CommandError(f"Not a valid export: {error}") from None
    return cmd_migrate(args)


# Parser ----------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bananawiki", description="BananaWiki administration.",
                                     epilog="Server lifecycle commands (install, update, backup, restore, …) "
                                            "are documented by 'bananawiki update --help' on a managed server.")
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Development server (loopback unless --host is given)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int, default=5001)
    serve.add_argument("--debug", action="store_true", help="Interactive debugger (loopback hosts only)")
    commands.add_parser("setup-token", help="Print the first-run setup token")
    commands.add_parser("migrate", help="Create or upgrade the database schema")
    db = commands.add_parser("db", help="Database maintenance")
    db_actions = db.add_subparsers(dest="db_action", required=True)
    db_actions.add_parser("migrate")
    db_actions.add_parser("check")
    backup = db_actions.add_parser("backup")
    backup.add_argument("--output")
    prune = db_actions.add_parser("prune-retired", help="Drop tables of retired 1.4 plugins (after a backup)")
    prune.add_argument("--yes", action="store_true")
    admin = commands.add_parser("create-admin", help="Create an administrator account")
    admin.add_argument("username")
    admin.add_argument("--owner", action="store_true")
    admin.add_argument("--password-file")
    reset = commands.add_parser("reset-password", help="Set a new password and end the account's sessions")
    reset.add_argument("username")
    group = reset.add_mutually_exclusive_group()
    group.add_argument("--password-file")
    group.add_argument("--generate", action="store_true", help="Generate a temporary password and print it")
    reset.add_argument("--temporary", action="store_true", help="Require a change at next sign-in")
    jobs = commands.add_parser("jobs", help="Background jobs")
    job_actions = jobs.add_subparsers(dest="jobs_action", required=True)
    job_actions.add_parser("list")
    run = job_actions.add_parser("run")
    run.add_argument("name", nargs="?")
    config = commands.add_parser("config", help="Configuration")
    config.add_argument("config_action", choices=("check",))
    export = commands.add_parser("export", help="Export the instance directory (database, files) to a package")
    export.add_argument("--output", required=True)
    importer = commands.add_parser("import", help="Import an export into an empty instance directory")
    importer.add_argument("package")
    return result


HANDLERS = {
    "serve": cmd_serve, "setup-token": cmd_setup_token, "migrate": cmd_migrate, "db": cmd_db,
    "create-admin": cmd_create_admin, "reset-password": cmd_reset_password, "jobs": cmd_jobs,
    "config": cmd_config, "export": cmd_export, "import": cmd_import,
}


def is_lifecycle(argv: list[str]) -> bool:
    first = next((item for item in argv if not item.startswith("-")), "")
    return any(item == "--root" or item.startswith("--root=") for item in argv) or first in LIFECYCLE_COMMANDS


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if is_lifecycle(arguments):
        from .ops.cli import main as lifecycle_main

        return lifecycle_main(arguments)
    args = parser().parse_args(arguments)
    try:
        return HANDLERS[args.command](args)
    except CommandError as error:
        print(f"bananawiki: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
