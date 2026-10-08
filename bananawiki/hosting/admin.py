"""Operator command line for the hosting platform (1.4 ``python -m hosting.admin_cli``).

::

    python -m bananawiki.hosting.admin --help
    sudo bananawiki hosting-admin instance list          # managed servers (runs as the service account)

It uses the same services as the web administration, so lifecycle side
effects (events, suspension audit, runtime calls, routing) are identical.
Differences from 1.4, which fix the audit findings:

* Passwords are never taken from the command line (visible in ``ps`` and the
  shell history): they are prompted for, read from ``--password-file`` or
  read from the first line of standard input when it is not a terminal.
* The audit log is written next to ``hosting.db`` (the data directory),
  not into the read-only release tree, and a failure to write it is reported.
* Output never includes password hashes, secrets or tokens.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.timeutil import MAX_YEAR, MIN_YEAR, parse, to_sql
from .errors import ServiceError
from .runtime import RuntimeFailure

AUDIT_FILE = "admin_cli_audit.log"
_PRIVATE_MARKERS = ("password", "secret", "token", "recovery", "totp")


class CliError(Exception):
    """A user-facing failure, printed without a traceback (exit status 1)."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: value for key, value in row.items() if not any(marker in key for marker in _PRIVATE_MARKERS)}


def _print(args: argparse.Namespace, data: Any, message: str | None = None) -> None:
    if message is not None and not args.json:
        print(message)
    else:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _read_password(args: argparse.Namespace, prompt: str) -> str:
    path = getattr(args, "password_file", None)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError, UnicodeDecodeError) as error:
            raise CliError(f"Cannot read a password from {path}.") from error
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat: ") != first:
        raise CliError("The passwords do not match.")
    return first


def _require_yes(args: argparse.Namespace, action: str) -> None:
    if not args.yes:
        raise CliError(f"{action} is destructive. Re-run with --yes to confirm.")


def _audit(action: str, payload: dict[str, Any]) -> None:
    """Append one line to ``<data dir>/admin_cli_audit.log`` (0600)."""
    from flask import current_app

    path = Path(current_app.config["HOSTING"].database_path).parent / AUDIT_FILE
    record = {"at": datetime.now(UTC).isoformat(), "actor": os.environ.get("SUDO_USER") or getpass.getuser(),
              "host": socket.gethostname(), "action": action, "payload": payload}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except OSError as error:
        print(f"Warning: the action was not written to the audit log {path}: {error}", file=sys.stderr)


def _account(ref: str) -> dict[str, Any]:
    from . import accounts

    found = accounts.get(ref) or accounts.by_username(ref)
    if found is None or found.get("deleted_at"):
        raise CliError(f"Account not found: {ref}")
    return found


def _instance(ref: str, domain_mode: str | None = None) -> dict[str, Any]:
    from . import instances
    from .db import db

    found = instances.get(ref)
    if found is None:
        modes = (domain_mode,) if domain_mode else ("hosting", "apex")
        for mode in modes:
            found = instances.by_slug(ref, mode)
            if found:
                break
    if found is None:
        found = db.one("SELECT * FROM instances WHERE subdomain = ? COLLATE NOCASE", (ref,))
    if found is None:
        raise CliError(f"Wiki not found: {ref}")
    return found


def _moment(value: str, name: str) -> datetime:
    """An ISO date and time (UTC unless it says otherwise) within the years the portal accepts."""
    if parse(value) is None:
        raise CliError(f"{name} must be an ISO date and time.")
    moment = parse(value, bounded=True)
    if moment is None:
        raise CliError(f"{name} must be between the years {MIN_YEAR} and {MAX_YEAR}.")
    return moment.astimezone(UTC)


def _until(args: argparse.Namespace) -> tuple[str | None, str]:
    if args.until:
        moment = _moment(args.until, "--until")
        if moment <= datetime.now(UTC):
            raise CliError("--until must be in the future.")
        return to_sql(moment), moment.strftime("%Y-%m-%d %H:%M UTC")
    if args.hours:
        if args.hours <= 0:
            raise CliError("--hours must be positive.")
        try:
            moment = datetime.now(UTC) + timedelta(hours=args.hours)
        except OverflowError:
            moment = None
        if moment is None or moment.year > MAX_YEAR:
            raise CliError(f"--hours is too large: the suspension would end after the year {MAX_YEAR}.")
        return to_sql(moment), f"{args.hours}h"
    return None, "permanent"


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> None:
    from . import instances, settings

    _print(args, {"stats": instances.platform_stats(), "settings": _public(settings.load())})


def cmd_account_list(args: argparse.Namespace) -> None:
    from . import accounts

    _print(args, [_public(row) for row in accounts.all_accounts()])


def cmd_account_show(args: argparse.Namespace) -> None:
    from . import instances

    account = _account(args.account)
    _print(args, {"account": _public(account), "wikis": [_public(row) for row in instances.owned_by(account["id"])]})


def cmd_account_create(args: argparse.Namespace) -> None:
    from . import accounts

    password = _read_password(args, f"Password for {args.username}: ")
    account = accounts.create(args.username, password, is_admin=args.admin)
    _audit("account.create", {"account_id": account["id"], "username": account["username"], "admin": args.admin})
    _print(args, _public(account), f"Created account {account['username']} ({account['id']}).")


def cmd_account_set_password(args: argparse.Namespace) -> None:
    from . import accounts

    account = _account(args.account)
    password = _read_password(args, f"New password for {account['username']}: ")
    accounts.check_new_password(password)
    accounts.set_password(account["id"], password, actor_id=None, reason="admin_cli")
    _audit("account.set_password", {"account_id": account["id"]})
    _print(args, {"ok": True, "account": account["id"]}, f"Password changed for {account['username']}; "
                                                         "their sessions and API tokens were revoked.")


def cmd_account_set_admin(args: argparse.Namespace) -> None:
    from . import accounts, instances

    account = _account(args.account)
    accounts.set_admin(account["id"], args.admin)
    for inst in instances.owned_by(account["id"]):
        instances.apply_owner_quota(inst, args.admin)
        instances.apply_policy(inst, actor_id=None)
    _audit("account.set_admin", {"account_id": account["id"], "admin": args.admin})
    _print(args, {"ok": True, "is_admin": args.admin},
           f"{account['username']} is {'now' if args.admin else 'no longer'} an administrator.")


def cmd_account_suspend(args: argparse.Namespace) -> None:
    from . import accounts, instances

    account = _account(args.account)
    until, label = _until(args)
    accounts.suspend(account["id"], until=until, reason=args.reason or "", reason_visible=args.reason_visible,
                     time_visible=args.time_visible, actor_id=None, duration_label=label)
    if args.also_instances:
        for inst in instances.owned_by(account["id"]):
            instances.suspend(inst, actor_id=None, until=until, reason=args.reason or "", duration_label=label)
    _audit("account.suspend", {"account_id": account["id"], "until": until, "also_instances": args.also_instances})
    _print(args, {"ok": True, "until": until}, f"Suspended {account['username']} ({label}).")


def cmd_account_unsuspend(args: argparse.Namespace) -> None:
    from . import accounts, instances

    account = _account(args.account)
    accounts.unsuspend(account["id"], actor_id=None)
    if args.also_instances:
        for inst in instances.owned_by(account["id"]):
            if inst["status"] == "suspended":
                instances.unsuspend(inst, actor_id=None)
    _audit("account.unsuspend", {"account_id": account["id"], "also_instances": args.also_instances})
    _print(args, {"ok": True}, f"Lifted the suspension of {account['username']}.")


def cmd_account_delete(args: argparse.Namespace) -> None:
    from . import accounts, instances

    account = _account(args.account)
    _require_yes(args, "Deleting an account")
    accounts.check_deletable(account["id"])
    instances.terminate_all(account["id"], actor_id=None)
    accounts.delete(account["id"])
    _audit("account.delete", {"account_id": account["id"], "username": account["username"]})
    _print(args, {"ok": True}, f"Deleted {account['username']}; their wikis were terminated.")


def cmd_instance_list(args: argparse.Namespace) -> None:
    from . import instances

    rows = instances.admin_list()
    if args.account:
        owner = _account(args.account)
        rows = [row for row in rows if row["account_id"] == owner["id"]]
    if args.status:
        rows = [row for row in rows if row["status"] == args.status]
    _print(args, [_public(row) for row in rows])


def cmd_instance_show(args: argparse.Namespace) -> None:
    from . import instances

    inst = _instance(args.instance, args.domain_mode)
    _print(args, _public(instances.describe(inst)))


def cmd_instance_create(args: argparse.Namespace) -> None:
    from . import instances

    owner = _account(args.account)
    password = _read_password(args, "Initial administrator password: ") if args.set_password else ""
    inst, username, generated = instances.create(owner, args.subdomain, domain_mode=args.domain_mode,
                                                 admin_username=args.admin_username or "", admin_password=password)
    _audit("instance.create", {"instance_id": inst["id"], "account_id": owner["id"], "slug": inst["subdomain"]})
    result = {"instance": _public(instances.describe(inst, with_status=False)), "admin_username": username}
    if not password:
        result["admin_password"] = generated
    _print(args, result, f"Created {inst['subdomain']} ({inst['id']}); administrator {username}"
                         + ("" if password else f", temporary password {generated}") + ".")


def _simple(action: str, run: Callable[[dict[str, Any]], Any], message: str) -> Callable[[argparse.Namespace], None]:
    def command(args: argparse.Namespace) -> None:
        inst = _instance(args.instance, args.domain_mode)
        if getattr(args, "destructive", False):
            _require_yes(args, message)
        run(inst)
        _audit(f"instance.{action}", {"instance_id": inst["id"], "slug": inst["subdomain"]})
        _print(args, {"ok": True, "instance": inst["id"]}, f"{message}: {inst['subdomain']}.")

    return command


def _instances() -> Any:
    from . import instances

    return instances


def cmd_instance_suspend(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    until, label = _until(args)
    _instances().suspend(inst, actor_id=None, until=until, reason=args.reason or "",
                         reason_visible=args.reason_visible, time_visible=args.time_visible, duration_label=label)
    _audit("instance.suspend", {"instance_id": inst["id"], "until": until})
    _print(args, {"ok": True, "until": until}, f"Suspended {inst['subdomain']} ({label}).")


def cmd_instance_extend(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    _instances().shift_expiry(inst, args.days * 86400, actor_id=None)
    _audit("instance.extend", {"instance_id": inst["id"], "days": args.days})
    _print(args, {"ok": True}, f"Extended {inst['subdomain']} by {args.days} day(s).")


def cmd_instance_set_expiry(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    moment = _moment(args.expires_at, "expires_at")
    _instances().set_expiry(inst, to_sql(moment), actor_id=None)
    _audit("instance.set_expiry", {"instance_id": inst["id"], "expires_at": to_sql(moment)})
    _print(args, {"ok": True}, f"{inst['subdomain']} now expires at {to_sql(moment)} UTC.")


def cmd_instance_storage_limit(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    if args.limit == "unlimited":
        limit = 0
    elif args.limit.isdigit() and int(args.limit) > 0:
        limit = int(args.limit)
    else:
        raise CliError("The limit is a number of MB or 'unlimited'.")
    _instances().set_storage_limit(inst, limit, actor_id=None)
    _audit("instance.storage_limit", {"instance_id": inst["id"], "limit_mb": limit})
    _print(args, {"ok": True, "limit_mb": limit}, f"Storage limit of {inst['subdomain']}: {args.limit}.")


def cmd_instance_transfer(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    target = _account(args.account)
    notes = _instances().move_to_owner(inst, target, actor_id=None)
    _audit("instance.transfer", {"instance_id": inst["id"], "account_id": target["id"], "notes": notes})
    _print(args, {"ok": True, "notes": notes}, f"{inst['subdomain']} now belongs to {target['username']}.")


def cmd_instance_rename(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.current_domain_mode)
    renamed = _instances().rename(inst, args.subdomain, args.domain_mode, actor_id=None, auto_suffix=args.auto_suffix)
    _audit("instance.rename", {"instance_id": inst["id"], "from": inst["subdomain"], "to": renamed["subdomain"]})
    _print(args, _public(renamed), f"Renamed {inst['subdomain']} to {renamed['subdomain']}.")


def cmd_instance_duplicate(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.current_domain_mode)
    owner = _account(args.account) if args.account else _account(inst["account_id"])
    copy = _instances().duplicate(inst, owner, args.subdomain, args.domain_mode, actor_id=None)
    _audit("instance.duplicate", {"instance_id": inst["id"], "copy_id": copy["id"]})
    _print(args, _public(copy), f"Copied {inst['subdomain']} to {copy['subdomain']} ({copy['id']}).")


def cmd_instance_restore(args: argparse.Namespace) -> None:
    inst = _instance(args.instance)
    restored = _instances().restore(inst, actor_id=None, extend_days=args.extend_days)
    _audit("instance.restore", {"instance_id": inst["id"], "slug": restored["subdomain"]})
    _print(args, _public(restored), f"Restored {restored['subdomain']} ({restored['status']}).")


def cmd_instance_import_archive(args: argparse.Namespace) -> None:
    owner = _account(args.account)
    if not owner["is_admin"] and not args.override:
        raise CliError("Imports are for administrator accounts; add --override to import for this account.")
    path = Path(args.archive)
    if not path.is_file():
        raise CliError(f"{path} is not a file.")
    inst = _instances().import_archive(owner, args.subdomain, args.domain_mode, path, actor_id=None)
    _audit("instance.import", {"instance_id": inst["id"], "account_id": owner["id"], "archive": path.name})
    _print(args, _public(inst), f"Imported {inst['subdomain']} ({inst['id']}).")


def cmd_instance_reset_password(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    username, password = _instances().reset_admin_password(inst, actor_id=None)
    _audit("instance.reset_password", {"instance_id": inst["id"], "username": username})
    _print(args, {"username": username, "password": password},
           f"Administrator {username} of {inst['subdomain']}: temporary password {password}")


def cmd_instance_reset_wiki(args: argparse.Namespace) -> None:
    inst = _instance(args.instance, args.domain_mode)
    _require_yes(args, "Resetting a wiki")
    username, password = _instances().reset_content(inst, actor_id=None)
    _audit("instance.reset_wiki", {"instance_id": inst["id"]})
    _print(args, {"username": username, "password": password},
           f"{inst['subdomain']} was reset; administrator {username}, temporary password {password}")


def cmd_instance_users(args: argparse.Namespace) -> None:
    instances = _instances()
    inst = _instance(args.instance, args.domain_mode)
    users, total = instances.runtime().list_users(instances.spec(inst, with_policy=False), limit=args.limit)
    _print(args, {"total": total, "users": [user.__dict__ for user in users]})


def cmd_instance_set_user_password(args: argparse.Namespace) -> None:
    instances = _instances()
    inst = _instance(args.instance, args.domain_mode)
    password = _read_password(args, f"Password for {args.username}: ")
    instances.runtime().set_user_password(instances.spec(inst, with_policy=False), args.username, password, args.role)
    _audit("instance.set_user_password", {"instance_id": inst["id"], "username": args.username, "role": args.role})
    _print(args, {"ok": True}, f"Password of {args.username} on {inst['subdomain']} set (role {args.role}).")


def cmd_instance_remove_user(args: argparse.Namespace) -> None:
    instances = _instances()
    inst = _instance(args.instance, args.domain_mode)
    _require_yes(args, "Removing a wiki user")
    instances.runtime().remove_user(instances.spec(inst, with_policy=False), args.username)
    _audit("instance.remove_user", {"instance_id": inst["id"], "username": args.username})
    _print(args, {"ok": True}, f"Removed {args.username} from {inst['subdomain']}.")


def cmd_settings_signup_mode(args: argparse.Namespace) -> None:
    from . import settings

    settings.update(signup_mode=args.mode)
    _audit("settings.signup_mode", {"mode": args.mode})
    _print(args, {"ok": True, "signup_mode": args.mode}, f"Sign-up mode: {args.mode}.")


def cmd_invite_list(args: argparse.Namespace) -> None:
    from . import invites

    _print(args, invites.list_all())


def cmd_invite_create(args: argparse.Namespace) -> None:
    from . import invites

    invite = invites.create("admin-cli", note=args.note, max_uses=args.max_uses)
    _audit("invite.create", {"id": invite["id"], "max_uses": args.max_uses})
    _print(args, invite, f"Invite code: {invite['code']}")


def cmd_invite_delete(args: argparse.Namespace) -> None:
    from . import invites

    if not invites.delete(args.invite_id):
        raise CliError("Invite code not found.")
    _audit("invite.delete", {"id": args.invite_id})
    _print(args, {"ok": True}, "Invite code revoked.")


INSTANCE_ACTIONS: dict[str, tuple[str, Callable[[Any, dict[str, Any]], Any], bool]] = {
    "stop": ("Stopped", lambda m, i: m.stop(i, actor_id=None, allow_suspended=True), False),
    "start": ("Started", lambda m, i: m.start(i, actor_id=None, allow_suspended=True), False),
    "restart": ("Restarted", lambda m, i: m.restart(i, actor_id=None), False),
    "force-restart": ("Force-restarted", lambda m, i: m.runtime().restart(m.spec(i), force=True), False),
    "unsuspend": ("Lifted the suspension of", lambda m, i: m.unsuspend(i, actor_id=None), False),
    "terminate": ("Terminated", lambda m, i: m.terminate(i, actor_id=None, reason="manual"), True),
    "hard-delete": ("Deleted", lambda m, i: m.hard_delete(i, actor_id=None), True),
    "indefinite": ("Removed the expiry of", lambda m, i: m.set_expiry(i, None, actor_id=None), False),
}


# ── Parser ────────────────────────────────────────────────────────────────────


def _instance_parser(sub: Any, name: str, handler: Callable[[argparse.Namespace], None], **kwargs: Any) -> Any:
    parser = sub.add_parser(name, **kwargs)
    parser.add_argument("instance", help="Wiki id or name")
    parser.add_argument("--domain-mode", choices=("hosting", "apex"))
    parser.set_defaults(handler=handler)
    return parser


def _password_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--password-file", help="Read the password from the first line of this file")


def _suspension_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hours", type=int)
    parser.add_argument("--until", help="ISO date and time")
    parser.add_argument("--reason", default="")
    parser.add_argument("--reason-visible", action="store_true")
    parser.add_argument("--time-visible", action="store_true")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bananawiki hosting-admin", description="BananaWiki Hosting administration.")
    root.add_argument("--json", action="store_true", help="Machine-readable output")
    groups = root.add_subparsers(dest="group", required=True)
    groups.add_parser("status", help="Platform statistics and settings").set_defaults(handler=cmd_status)

    account = groups.add_parser("account", help="Hosting accounts").add_subparsers(dest="action", required=True)
    account.add_parser("list").set_defaults(handler=cmd_account_list)
    p = account.add_parser("show")
    p.add_argument("account")
    p.set_defaults(handler=cmd_account_show)
    p = account.add_parser("create")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true")
    _password_option(p)
    p.set_defaults(handler=cmd_account_create)
    p = account.add_parser("set-password")
    p.add_argument("account")
    _password_option(p)
    p.set_defaults(handler=cmd_account_set_password)
    p = account.add_parser("set-admin")
    p.add_argument("account")
    state = p.add_mutually_exclusive_group(required=True)
    state.add_argument("--admin", dest="admin", action="store_true")
    state.add_argument("--no-admin", dest="admin", action="store_false")
    p.set_defaults(handler=cmd_account_set_admin)
    p = account.add_parser("suspend")
    p.add_argument("account")
    _suspension_options(p)
    p.add_argument("--also-instances", action="store_true")
    p.set_defaults(handler=cmd_account_suspend)
    p = account.add_parser("unsuspend")
    p.add_argument("account")
    p.add_argument("--also-instances", action="store_true")
    p.set_defaults(handler=cmd_account_unsuspend)
    p = account.add_parser("delete")
    p.add_argument("account")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(handler=cmd_account_delete)

    inst = groups.add_parser("instance", help="Hosted wikis").add_subparsers(dest="action", required=True)
    p = inst.add_parser("list")
    p.add_argument("--account")
    p.add_argument("--status", choices=("running", "stopped", "suspended", "terminated"))
    p.set_defaults(handler=cmd_instance_list)
    _instance_parser(inst, "show", cmd_instance_show)
    p = inst.add_parser("create")
    p.add_argument("account")
    p.add_argument("subdomain")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--admin-username")
    p.add_argument("--set-password", action="store_true", help="Choose the administrator password (else generated)")
    _password_option(p)
    p.set_defaults(handler=cmd_instance_create)
    for name, (message, run, destructive) in INSTANCE_ACTIONS.items():
        p = _instance_parser(inst, name, _simple(name.replace("-", "_"), lambda i, run=run: run(_instances(), i),
                                                 message))
        p.set_defaults(destructive=destructive)
        if destructive:
            p.add_argument("--yes", action="store_true")
    _suspension_options(_instance_parser(inst, "suspend", cmd_instance_suspend))
    _instance_parser(inst, "extend", cmd_instance_extend).add_argument("days", type=int)
    _instance_parser(inst, "set-expiry", cmd_instance_set_expiry).add_argument("expires_at")
    _instance_parser(inst, "storage-limit", cmd_instance_storage_limit).add_argument("limit", help="MB or 'unlimited'")
    _instance_parser(inst, "transfer", cmd_instance_transfer).add_argument("account")
    for name, handler in (("rename", cmd_instance_rename), ("duplicate", cmd_instance_duplicate)):
        p = inst.add_parser(name)
        p.add_argument("instance")
        p.add_argument("subdomain")
        p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
        p.add_argument("--current-domain-mode", choices=("hosting", "apex"))
        if name == "rename":
            p.add_argument("--auto-suffix", action="store_true")
        else:
            p.add_argument("--account", help="Owner of the copy (default: the same owner)")
        p.set_defaults(handler=handler)
    p = inst.add_parser("restore")
    p.add_argument("instance")
    p.add_argument("--extend-days", type=int)
    p.set_defaults(handler=cmd_instance_restore)
    p = inst.add_parser("import-archive")
    p.add_argument("account")
    p.add_argument("subdomain")
    p.add_argument("archive", help="A BananaWiki ZIP archive on this server")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--override", action="store_true", help="Allow an import for a non-administrator account")
    p.set_defaults(handler=cmd_instance_import_archive)
    _instance_parser(inst, "reset-password", cmd_instance_reset_password)
    _instance_parser(inst, "reset-wiki", cmd_instance_reset_wiki).add_argument("--yes", action="store_true")
    _instance_parser(inst, "users", cmd_instance_users).add_argument("--limit", type=int, default=200)
    p = _instance_parser(inst, "set-user-password", cmd_instance_set_user_password)
    p.add_argument("username")
    p.add_argument("--role", choices=("user", "editor", "admin", "owner"), default="user")
    _password_option(p)
    p = _instance_parser(inst, "remove-user", cmd_instance_remove_user)
    p.add_argument("username")
    p.add_argument("--yes", action="store_true")

    settings = groups.add_parser("settings", help="Platform settings").add_subparsers(dest="action", required=True)
    p = settings.add_parser("signup-mode")
    p.add_argument("mode", choices=("open", "invite", "approval", "closed"))
    p.set_defaults(handler=cmd_settings_signup_mode)

    invite = groups.add_parser("invite", help="Sign-up invite codes").add_subparsers(dest="action", required=True)
    invite.add_parser("list").set_defaults(handler=cmd_invite_list)
    p = invite.add_parser("create")
    p.add_argument("--note", default="")
    p.add_argument("--max-uses", type=int, default=1)
    p.set_defaults(handler=cmd_invite_create)
    p = invite.add_parser("delete")
    p.add_argument("invite_id", type=int)
    p.set_defaults(handler=cmd_invite_delete)
    return root


@contextmanager
def _portal(app: Any = None) -> Iterator[Any]:
    """A portal application with a database connection bound to this thread."""
    from .app import create_app
    from .db import connection_scope

    app = app or create_app()
    with app.test_request_context("/"), connection_scope(app.extensions["bananawiki.hosting.database"]):
        yield app


def main(argv: list[str] | None = None, *, app: Any = None) -> int:
    args = parser().parse_args(argv)
    try:
        with _portal(app):
            from .i18n import t

            try:
                args.handler(args)
            except ServiceError as error:
                raise CliError(t(error.key, **error.values)) from None
            except RuntimeFailure as error:
                raise CliError(f"{t(f'hosting.runtime.{error.code}')} ({error.detail})") from None
    except CliError as error:
        print(f"hosting-admin: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
