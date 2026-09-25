"""Internal SSH-only administration CLI for the hosting platform.

Run with::

    python -m hosting.admin_cli --help

This command intentionally bypasses the Flask session / CSRF layer because it
is meant for operators who already have shell access to the host.  It still
uses the same hosting database and instance-manager helpers as the web admin UI
so lifecycle side effects stay consistent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from helpers._passwords import generate_password_hash

from . import config
from .db import (
    change_account_password,
    create_account,
    create_invite_code,
    delete_account,
    delete_invite_code,
    get_account_by_id,
    get_account_by_username,
    get_all_accounts,
    get_all_instances,
    get_hosting_settings,
    get_instance,
    get_instance_by_subdomain,
    get_instances_for_account,
    init_hosting_db,
    list_invite_codes,
    record_account_suspension_action,
    record_instance_suspension_action,
    set_account_admin,
    suspend_account,
    transfer_instance_ownership,
    unsuspend_account,
    update_hosting_settings,
)
from .instance_manager import (
    apply_account_owner_quota_policy,
    apply_instance_owner_quota_policy,
    change_instance_identity,
    convert_account_apex_instances_to_hosting,
    duplicate_instance,
    extend_instance,
    force_restart_instance,
    get_admin_instances,
    get_instance_detail,
    get_platform_stats,
    hard_delete_terminated_instance,
    list_instance_users,
    make_instance_indefinite,
    provision_instance_from_archive,
    provision_instance,
    remove_instance_user,
    reset_instance_password,
    reset_wiki,
    restart_instance,
    restore_instance,
    set_instance_expiry,
    set_instance_storage_limit,
    set_instance_user_password,
    stop_instance,
    suspend_instance,
    terminate_instance,
    unsuspend_instance,
)


class CliError(RuntimeError):
    """User-facing command error."""


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return {key: row[key] for key in row.keys()}
    except AttributeError:
        return dict(row)


def _print(args: argparse.Namespace, data: Any, *, message: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    elif message is not None:
        print(message)
    else:
        if isinstance(data, (dict, list)):
            print(json.dumps(data, indent=2, sort_keys=True, default=str))
        else:
            print(data)


def _require_yes(args: argparse.Namespace, action: str) -> None:
    if getattr(args, "yes", False):
        return
    raise CliError(f"{action} is destructive. Re-run with --yes to confirm.")


def _apply_config_overrides(args: argparse.Namespace) -> None:
    if getattr(args, "hosting_db", None):
        config.HOSTING_DATABASE_PATH = os.path.abspath(args.hosting_db)
    if getattr(args, "instances_dir", None):
        config.INSTANCES_DIR = os.path.abspath(args.instances_dir)


def _audit(action: str, payload: dict[str, Any]) -> None:
    """Append a best-effort local audit line for SSH override actions."""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        actor = getpass.getuser()
        host = socket.gethostname()
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "host": host,
            "action": action,
            "payload": payload,
        }
        # The log names accounts and instances and sits beside the 0600 key
        # material, so it must not be world readable.
        log_path = os.path.join(data_dir, "admin_cli_audit.log")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        # The command result matters more than audit logging.  This is a local
        # fallback log, not the source of truth for hosting state.
        pass


def _resolve_account(ref: str) -> dict[str, Any]:
    row = get_account_by_id(ref) or get_account_by_username(ref)
    account = _row_to_dict(row)
    if account is None:
        raise CliError(f"Account not found: {ref}")
    return account


def _resolve_instance(ref: str, *, domain_mode: str | None = None) -> dict[str, Any]:
    row = get_instance(ref)
    if row is None and domain_mode:
        row = get_instance_by_subdomain(ref, domain_mode=domain_mode)
    if row is None and not domain_mode:
        row = get_instance_by_subdomain(ref)
    if row is None:
        for candidate in get_all_instances():
            cand = _row_to_dict(candidate)
            if not cand:
                continue
            if cand["subdomain"].lower() != ref.lower():
                continue
            if domain_mode and cand.get("domain_mode") != domain_mode:
                continue
            row = candidate
            break
    inst = _row_to_dict(row)
    if inst is None:
        suffix = f" ({domain_mode})" if domain_mode else ""
        raise CliError(f"Instance not found: {ref}{suffix}")
    return inst


def _parse_until(args: argparse.Namespace) -> tuple[str | None, str]:
    if getattr(args, "until", None):
        try:
            until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CliError("--until must be an ISO datetime") from exc
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        until = until.astimezone(timezone.utc)
        if until <= datetime.now(timezone.utc):
            raise CliError("--until must be in the future")
        return until.isoformat(), until.strftime("%Y-%m-%d %H:%M UTC")
    if getattr(args, "hours", None):
        if args.hours <= 0:
            raise CliError("--hours must be positive")
        until = datetime.now(timezone.utc) + timedelta(hours=args.hours)
        return until.isoformat(), f"{args.hours}h"
    return None, "permanent"


def _parse_expiry(raw: str) -> datetime:
    try:
        exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliError("Expiry must be an ISO datetime") from exc
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp.astimezone(timezone.utc)


def _ok_or_error(ok: bool, reason: str, success: str) -> str:
    if not ok:
        raise CliError(reason or "Operation failed.")
    return success


def cmd_status(args: argparse.Namespace) -> None:
    stats = get_platform_stats()
    settings = _row_to_dict(get_hosting_settings())
    data = {"stats": stats, "settings": settings}
    _print(args, data, message=json.dumps(data, indent=2, default=str))


def cmd_account_list(args: argparse.Namespace) -> None:
    rows = [_row_to_dict(row) for row in get_all_accounts()]
    _print(args, rows)


def cmd_account_show(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    instances = [_row_to_dict(row) for row in get_instances_for_account(account["id"])]
    _print(args, {"account": account, "instances": instances})


def cmd_account_create(args: argparse.Namespace) -> None:
    if get_account_by_username(args.username):
        raise CliError(f"Account already exists: {args.username}")
    account_id = create_account(
        args.username,
        generate_password_hash(args.password),
        is_admin=args.admin,
    )
    _audit("account.create", {"account_id": account_id, "username": args.username, "admin": args.admin})
    _print(args, {"id": account_id, "username": args.username, "is_admin": args.admin},
           message=f"Created account {args.username} ({account_id}).")


def cmd_account_set_password(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    change_account_password(account["id"], generate_password_hash(args.password))
    _audit("account.set_password", {"account_id": account["id"], "username": account["username"]})
    _print(args, {"ok": True, "account": account["id"]},
           message=f"Password updated for {account['username']}.")


def cmd_account_set_admin(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    make_admin = bool(args.admin)
    set_account_admin(account["id"], make_admin)
    apply_account_owner_quota_policy(account["id"], owner_is_admin=make_admin)
    converted = []
    if not make_admin:
        converted = convert_account_apex_instances_to_hosting(account["id"])
    _audit(
        "account.set_admin",
        {
            "account_id": account["id"],
            "username": account["username"],
            "admin": make_admin,
            "converted_apex_instances": len(converted),
        },
    )
    label = "admin" if make_admin else "regular user"
    data = {
        "ok": True,
        "account": account["id"],
        "is_admin": make_admin,
        "converted_apex_instances": [
            {"instance_id": iid, "old_subdomain": old, "new_subdomain": new}
            for iid, old, new in converted
        ],
    }
    note = ""
    if converted:
        note = f" Converted {len(converted)} apex instance(s) to hosting mode."
    _print(args, data, message=f"{account['username']} is now {label}.{note}")


def cmd_account_suspend(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    suspended_until, duration = _parse_until(args)
    reason = (args.reason or "").strip()
    suspend_account(
        account["id"],
        reason=reason,
        suspended_until=suspended_until,
        reason_visible=args.reason_visible and bool(reason),
        time_visible=args.time_visible and suspended_until is not None,
        performed_by="ssh-cli",
    )
    record_account_suspension_action(
        account["id"],
        "suspend",
        performed_by="ssh-cli",
        reason=reason or None,
        reason_visible=args.reason_visible and bool(reason),
        time_visible=args.time_visible and suspended_until is not None,
        duration=duration,
        suspended_until=suspended_until,
    )
    affected = 0
    if args.also_instances:
        for inst in get_instances_for_account(account["id"]):
            if inst["status"] in ("running", "stopped"):
                ok, _ = suspend_instance(inst["id"], performed_by="ssh-cli")
                if ok:
                    affected += 1
    _audit("account.suspend", {"account_id": account["id"], "instances": affected, "until": suspended_until})
    _print(args, {"ok": True, "account": account["id"], "instances_suspended": affected},
           message=f"Suspended {account['username']}; {affected} instance(s) suspended.")


def cmd_account_unsuspend(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    unsuspend_account(account["id"])
    record_account_suspension_action(account["id"], "unsuspend", performed_by="ssh-cli")
    affected = 0
    if args.also_instances:
        for inst in get_instances_for_account(account["id"]):
            if inst["status"] == "suspended":
                ok, _ = unsuspend_instance(inst["id"])
                if ok:
                    affected += 1
    _audit("account.unsuspend", {"account_id": account["id"], "instances": affected})
    _print(args, {"ok": True, "account": account["id"], "instances_unsuspended": affected},
           message=f"Unsuspended {account['username']}; {affected} instance(s) unsuspended.")


def cmd_account_delete(args: argparse.Namespace) -> None:
    _require_yes(args, "Deleting an account")
    account = _resolve_account(args.account)
    terminated = 0
    for inst in get_instances_for_account(account["id"]):
        if inst["status"] != "terminated":
            ok, _ = terminate_instance(inst["id"])
            if ok:
                terminated += 1
    delete_account(account["id"])
    _audit("account.delete", {"account_id": account["id"], "username": account["username"], "terminated": terminated})
    _print(args, {"ok": True, "account": account["id"], "terminated_instances": terminated},
           message=f"Deleted account {account['username']} and terminated {terminated} instance(s).")


def cmd_instance_list(args: argparse.Namespace) -> None:
    if args.account:
        account = _resolve_account(args.account)
        rows = get_instances_for_account(account["id"])
    elif args.raw:
        rows = get_all_instances()
    else:
        rows = get_admin_instances()
    data = [_row_to_dict(row) for row in rows]
    if args.status:
        data = [row for row in data if row and row.get("status") == args.status]
    _print(args, data)


def cmd_instance_show(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    detail = get_instance_detail(inst["id"]) or inst
    _print(args, _row_to_dict(detail))


def cmd_instance_create(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    owner_is_admin = bool(account["is_admin"]) or args.override
    inst, error = provision_instance(
        account["id"],
        args.subdomain,
        domain_mode=args.domain_mode,
        account_is_admin=owner_is_admin,
        admin_username=args.admin_username,
        admin_password=args.admin_password,
    )
    if error:
        raise CliError(error)
    _audit("instance.create", {"instance_id": inst["id"], "account_id": account["id"], "override": args.override})
    _print(args, inst, message=f"Created instance {inst['subdomain']} ({inst['id']}) at {inst['url']}.")


def _instance_action(
    args: argparse.Namespace,
    action: str,
    func: Callable[..., tuple[bool, str]],
    success: str,
    *func_args: Any,
    destructive: bool = False,
    **func_kwargs: Any,
) -> None:
    if destructive:
        _require_yes(args, success)
    inst = _resolve_instance(args.instance, domain_mode=getattr(args, "domain_mode", None))
    ok, reason = func(inst["id"], *func_args, **func_kwargs)
    message = _ok_or_error(ok, reason, success.format(subdomain=inst["subdomain"], id=inst["id"]))
    _audit(f"instance.{action}", {"instance_id": inst["id"], "subdomain": inst["subdomain"]})
    _print(args, {"ok": True, "instance": inst["id"]}, message=message)


def cmd_instance_stop(args: argparse.Namespace) -> None:
    _instance_action(args, "stop", stop_instance, "Stopped {subdomain}.", allow_suspended=True)


def cmd_instance_restart(args: argparse.Namespace) -> None:
    _instance_action(args, "restart", restart_instance, "Restarted {subdomain}.", allow_suspended=True)


def cmd_instance_force_restart(args: argparse.Namespace) -> None:
    _instance_action(args, "force_restart", force_restart_instance, "Force-restarted {subdomain}.")


def cmd_instance_suspend(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    suspended_until, duration = _parse_until(args)
    reason = (args.reason or "").strip()
    ok, reason_msg = suspend_instance(
        inst["id"],
        suspended_until=suspended_until,
        reason=reason,
        reason_visible=args.reason_visible and bool(reason),
        time_visible=args.time_visible and suspended_until is not None,
        performed_by="ssh-cli",
    )
    _ok_or_error(ok, reason_msg, "")
    record_instance_suspension_action(
        inst["id"],
        "suspend",
        performed_by="ssh-cli",
        reason=reason or None,
        reason_visible=args.reason_visible and bool(reason),
        time_visible=args.time_visible and suspended_until is not None,
        duration=duration,
        suspended_until=suspended_until,
    )
    _audit("instance.suspend", {"instance_id": inst["id"], "until": suspended_until})
    _print(args, {"ok": True, "instance": inst["id"]}, message=f"Suspended {inst['subdomain']}.")


def cmd_instance_unsuspend(args: argparse.Namespace) -> None:
    _instance_action(args, "unsuspend", unsuspend_instance, "Unsuspended {subdomain}.")


def cmd_instance_terminate(args: argparse.Namespace) -> None:
    _instance_action(args, "terminate", terminate_instance, "Terminated {subdomain}.", destructive=True)


def cmd_instance_hard_delete(args: argparse.Namespace) -> None:
    _instance_action(args, "hard_delete", hard_delete_terminated_instance, "Permanently deleted {subdomain}.", destructive=True)


def cmd_instance_extend(args: argparse.Namespace) -> None:
    _instance_action(args, "extend", extend_instance, "Extended {subdomain}.", args.days)


def cmd_instance_indefinite(args: argparse.Namespace) -> None:
    _instance_action(args, "make_indefinite", make_instance_indefinite, "Set {subdomain} to run indefinitely.")


def cmd_instance_set_expiry(args: argparse.Namespace) -> None:
    exp = _parse_expiry(args.expires_at)
    _instance_action(args, "set_expiry", set_instance_expiry, "Updated expiry for {subdomain}.", exp)


def cmd_instance_storage_limit(args: argparse.Namespace) -> None:
    limit = 0 if args.limit.lower() == "unlimited" else int(args.limit)
    if limit < 0:
        raise CliError("Storage limit must be non-negative or 'unlimited'.")
    _instance_action(args, "storage_limit", set_instance_storage_limit, "Updated storage limit for {subdomain}.", limit)


def cmd_instance_transfer(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    account = _resolve_account(args.account)
    old_owner = _row_to_dict(get_account_by_id(inst["account_id"]))
    target_is_admin = bool(account["is_admin"])
    converted = None
    if inst.get("domain_mode") == "apex" and not target_is_admin:
        converted, error = change_instance_identity(
            inst["id"],
            inst["subdomain"],
            "hosting",
            account_is_admin=False,
            auto_suffix=True,
        )
        if error:
            raise CliError(error)
        inst = _resolve_instance(inst["id"])
    if not transfer_instance_ownership(inst["id"], account["id"]):
        raise CliError("Transfer failed.")
    old_owner_is_admin = bool(old_owner and old_owner["is_admin"])
    if old_owner_is_admin != target_is_admin:
        ok, reason = apply_instance_owner_quota_policy(inst["id"], owner_is_admin=target_is_admin)
        if not ok:
            raise CliError(reason)
    _audit(
        "instance.transfer",
        {
            "instance_id": inst["id"],
            "account_id": account["id"],
            "converted_to_hosting": bool(converted),
        },
    )
    data = {
        "ok": True,
        "instance": inst["id"],
        "account": account["id"],
        "converted_to_hosting": converted,
    }
    note = " Converted apex instance to hosting mode." if converted else ""
    _print(args, data, message=f"Transferred {inst['subdomain']} to {account['username']}.{note}")


def cmd_instance_rename(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.current_domain_mode)
    owner = get_account_by_id(inst["account_id"])
    owner_is_admin = bool(owner and owner["is_admin"]) or args.override
    new_inst, error = change_instance_identity(
        inst["id"],
        args.subdomain,
        args.domain_mode,
        account_is_admin=owner_is_admin,
        auto_suffix=args.auto_suffix,
    )
    if error:
        raise CliError(error)
    _audit("instance.rename", {"instance_id": inst["id"], "subdomain": args.subdomain, "domain_mode": args.domain_mode})
    _print(args, new_inst, message=f"Renamed {inst['subdomain']} to {new_inst['subdomain']}.")


def cmd_instance_duplicate(args: argparse.Namespace) -> None:
    source = _resolve_instance(args.instance, domain_mode=args.current_domain_mode)
    account = _resolve_account(args.account) if args.account else _row_to_dict(get_account_by_id(source["account_id"]))
    if account is None:
        raise CliError("Target account not found.")
    owner_is_admin = bool(account["is_admin"]) or args.override
    new_inst, error = duplicate_instance(
        source["id"],
        account["id"],
        args.subdomain,
        domain_mode=args.domain_mode,
        account_is_admin=owner_is_admin,
    )
    if error:
        raise CliError(error)
    _audit("instance.duplicate", {"source_instance_id": source["id"], "new_instance_id": new_inst["id"]})
    _print(args, new_inst, message=f"Duplicated {source['subdomain']} to {new_inst['subdomain']}.")


def cmd_instance_restore(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    new_inst, error = restore_instance(inst["id"], extend_days=args.extend_days)
    if error:
        raise CliError(error)
    _audit("instance.restore", {"source_instance_id": inst["id"], "new_instance_id": new_inst["id"]})
    _print(args, new_inst, message=f"Restored {new_inst['subdomain']} ({new_inst['id']}).")


def cmd_instance_import_archive(args: argparse.Namespace) -> None:
    account = _resolve_account(args.account)
    archive_path = os.path.abspath(args.archive)
    if not os.path.isfile(archive_path):
        raise CliError(f"Archive not found: {archive_path}")
    account_is_admin = bool(account["is_admin"]) or args.override
    new_inst, error = provision_instance_from_archive(
        account["id"],
        args.subdomain,
        archive_path,
        domain_mode=args.domain_mode,
        account_is_admin=account_is_admin,
    )
    if error:
        raise CliError(error)
    _audit(
        "instance.import_archive",
        {
            "account_id": account["id"],
            "archive_path": archive_path,
            "new_instance_id": new_inst["id"],
        },
    )
    _print(args, new_inst, message=f"Imported {new_inst['subdomain']} ({new_inst['id']}).")


def cmd_instance_reset_password(args: argparse.Namespace) -> None:
    _instance_action(args, "reset_password", reset_instance_password, "Reset wiki admin password for {subdomain}.")


def cmd_instance_reset_wiki(args: argparse.Namespace) -> None:
    _instance_action(args, "reset_wiki", reset_wiki, "Reset wiki content for {subdomain}.", destructive=True)


def cmd_instance_users(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    _print(args, list_instance_users(inst["id"]))


def cmd_instance_set_user_password(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    ok, reason = set_instance_user_password(inst["id"], args.username, args.password, role=args.role)
    _ok_or_error(ok, reason, "")
    _audit("instance.user.set_password", {"instance_id": inst["id"], "username": args.username, "role": args.role})
    _print(args, {"ok": True}, message=f"Updated {args.username} on {inst['subdomain']}.")


def cmd_instance_remove_user(args: argparse.Namespace) -> None:
    inst = _resolve_instance(args.instance, domain_mode=args.domain_mode)
    ok, reason = remove_instance_user(inst["id"], args.username)
    _ok_or_error(ok, reason, "")
    _audit("instance.user.remove", {"instance_id": inst["id"], "username": args.username})
    _print(args, {"ok": True}, message=f"Removed {args.username} from {inst['subdomain']}.")














def cmd_settings_signup_mode(args: argparse.Namespace) -> None:
    update_hosting_settings(signup_mode=args.mode)
    _audit("settings.signup_mode", {"mode": args.mode})
    _print(args, {"ok": True, "signup_mode": args.mode}, message=f"Signup mode set to {args.mode}.")


def cmd_invite_list(args: argparse.Namespace) -> None:
    _print(args, [_row_to_dict(row) for row in list_invite_codes()])


def cmd_invite_create(args: argparse.Namespace) -> None:
    row = create_invite_code(created_by="ssh-cli", note=args.note or "", max_uses=args.max_uses)
    data = _row_to_dict(row)
    _audit("invite.create", {"id": data.get("id"), "max_uses": args.max_uses})
    _print(args, data, message=f"Created invite code: {data['code']}")


def cmd_invite_delete(args: argparse.Namespace) -> None:
    removed = delete_invite_code(args.invite_id)
    if not removed:
        raise CliError("Invite code not found.")
    _audit("invite.delete", {"id": args.invite_id})
    _print(args, {"ok": True, "id": args.invite_id}, message="Invite code revoked.")


def _add_domain_mode_arg(parser: argparse.ArgumentParser, *, name: str = "--domain-mode") -> None:
    parser.add_argument(name, choices=("hosting", "apex"), default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hosting-admin",
        description="Internal SSH override CLI for BananaWiki hosting administration.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--hosting-db", help="Override HOSTING_DATABASE_PATH for this run.")
    parser.add_argument("--instances-dir", help="Override INSTANCES_DIR for this run.")
    sub = parser.add_subparsers(dest="area", required=True)

    status = sub.add_parser("status", help="Show platform stats and settings.")
    status.set_defaults(func=cmd_status)

    account = sub.add_parser("account", help="Manage hosting accounts.")
    account_sub = account.add_subparsers(dest="account_cmd", required=True)
    p = account_sub.add_parser("list")
    p.set_defaults(func=cmd_account_list)
    p = account_sub.add_parser("show")
    p.add_argument("account")
    p.set_defaults(func=cmd_account_show)
    p = account_sub.add_parser("create")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("--admin", action="store_true")
    p.set_defaults(func=cmd_account_create)
    p = account_sub.add_parser("set-password")
    p.add_argument("account")
    p.add_argument("password")
    p.set_defaults(func=cmd_account_set_password)
    p = account_sub.add_parser("set-admin")
    p.add_argument("account")
    state = p.add_mutually_exclusive_group(required=True)
    state.add_argument("--admin", dest="admin", action="store_true")
    state.add_argument("--no-admin", dest="admin", action="store_false")
    p.set_defaults(func=cmd_account_set_admin)
    p = account_sub.add_parser("suspend")
    p.add_argument("account")
    p.add_argument("--hours", type=int)
    p.add_argument("--until")
    p.add_argument("--reason")
    p.add_argument("--reason-visible", action="store_true")
    p.add_argument("--time-visible", action="store_true")
    p.add_argument("--also-instances", action="store_true")
    p.set_defaults(func=cmd_account_suspend)
    p = account_sub.add_parser("unsuspend")
    p.add_argument("account")
    p.add_argument("--also-instances", action="store_true")
    p.set_defaults(func=cmd_account_unsuspend)
    p = account_sub.add_parser("delete")
    p.add_argument("account")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_account_delete)

    instance = sub.add_parser("instance", help="Manage hosted wiki instances.")
    inst_sub = instance.add_subparsers(dest="instance_cmd", required=True)
    p = inst_sub.add_parser("list")
    p.add_argument("--account")
    p.add_argument("--status", choices=("running", "stopped", "suspended", "terminated"))
    p.add_argument("--raw", action="store_true", help="List raw DB rows instead of dashboard rows.")
    p.set_defaults(func=cmd_instance_list)
    p = inst_sub.add_parser("show")
    p.add_argument("instance")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_show)
    p = inst_sub.add_parser("create")
    p.add_argument("account")
    p.add_argument("subdomain")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--admin-username")
    p.add_argument("--admin-password")
    p.add_argument("--override", action="store_true", help="Bypass owner quota/apex checks for SSH recovery.")
    p.set_defaults(func=cmd_instance_create)
    for name, func in {
        "stop": cmd_instance_stop,
        "restart": cmd_instance_restart,
        "force-restart": cmd_instance_force_restart,
        "unsuspend": cmd_instance_unsuspend,
        "reset-password": cmd_instance_reset_password,
        "make-indefinite": cmd_instance_indefinite,
    }.items():
        p = inst_sub.add_parser(name)
        p.add_argument("instance")
        _add_domain_mode_arg(p)
        p.set_defaults(func=func)
    p = inst_sub.add_parser("suspend")
    p.add_argument("instance")
    _add_domain_mode_arg(p)
    p.add_argument("--hours", type=int)
    p.add_argument("--until")
    p.add_argument("--reason")
    p.add_argument("--reason-visible", action="store_true")
    p.add_argument("--time-visible", action="store_true")
    p.set_defaults(func=cmd_instance_suspend)
    for name, func in {
        "terminate": cmd_instance_terminate,
        "hard-delete": cmd_instance_hard_delete,
        "reset-wiki": cmd_instance_reset_wiki,
    }.items():
        p = inst_sub.add_parser(name)
        p.add_argument("instance")
        _add_domain_mode_arg(p)
        p.add_argument("--yes", action="store_true")
        p.set_defaults(func=func)
    p = inst_sub.add_parser("extend")
    p.add_argument("instance")
    p.add_argument("days", type=int)
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_extend)
    p = inst_sub.add_parser("set-expiry")
    p.add_argument("instance")
    p.add_argument("expires_at")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_set_expiry)
    p = inst_sub.add_parser("storage-limit")
    p.add_argument("instance")
    p.add_argument("limit", help="MB, or 'unlimited'.")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_storage_limit)
    p = inst_sub.add_parser("transfer")
    p.add_argument("instance")
    p.add_argument("account")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_transfer)
    p = inst_sub.add_parser("rename")
    p.add_argument("instance")
    p.add_argument("subdomain")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--current-domain-mode", choices=("hosting", "apex"))
    p.add_argument("--auto-suffix", action="store_true")
    p.add_argument("--override", action="store_true")
    p.set_defaults(func=cmd_instance_rename)
    p = inst_sub.add_parser("duplicate")
    p.add_argument("instance")
    p.add_argument("subdomain")
    p.add_argument("--account")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--current-domain-mode", choices=("hosting", "apex"))
    p.add_argument("--override", action="store_true")
    p.set_defaults(func=cmd_instance_duplicate)
    p = inst_sub.add_parser("restore")
    p.add_argument("instance")
    p.add_argument("--extend-days", type=int)
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_restore)
    p = inst_sub.add_parser("import-archive")
    p.add_argument("account", help="Target hosting account id or username.")
    p.add_argument("subdomain")
    p.add_argument("archive", help="Path to a BananaWiki ZIP archive already on the server.")
    p.add_argument("--domain-mode", choices=("hosting", "apex"), default="hosting")
    p.add_argument("--override", action="store_true", help="Allow operator import for a non-admin target account.")
    p.set_defaults(func=cmd_instance_import_archive)
    p = inst_sub.add_parser("users")
    p.add_argument("instance")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_users)
    p = inst_sub.add_parser("set-user-password")
    p.add_argument("instance")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("--role", choices=("user", "editor", "admin", "owner"), default="user")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_set_user_password)
    p = inst_sub.add_parser("remove-user")
    p.add_argument("instance")
    p.add_argument("username")
    _add_domain_mode_arg(p)
    p.set_defaults(func=cmd_instance_remove_user)

    settings = sub.add_parser("settings", help="Manage hosting settings.")
    settings_sub = settings.add_subparsers(dest="settings_cmd", required=True)
    p = settings_sub.add_parser("signup-mode")
    p.add_argument("mode", choices=("open", "invite", "closed"))
    p.set_defaults(func=cmd_settings_signup_mode)

    invite = sub.add_parser("invite", help="Manage signup invite codes.")
    invite_sub = invite.add_subparsers(dest="invite_cmd", required=True)
    p = invite_sub.add_parser("list")
    p.set_defaults(func=cmd_invite_list)
    p = invite_sub.add_parser("create")
    p.add_argument("--note", default="")
    p.add_argument("--max-uses", type=int, default=1)
    p.set_defaults(func=cmd_invite_create)
    p = invite_sub.add_parser("delete")
    p.add_argument("invite_id", type=int)
    p.set_defaults(func=cmd_invite_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_config_overrides(args)
    init_hosting_db()
    try:
        args.func(args)
        return 0
    except CliError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
