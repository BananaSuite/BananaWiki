"""``banana`` / the installed ``bananawiki`` command: one entry point for server maintenance.

Same subcommands and options as 1.4 (C13). Additions: ``agent serve|status``
(hosting runtime agent), ``proxy --email``, and pass-through of application
administration commands (``create-admin``, ``reset-password``, ``db …``,
``jobs …``, ``config check``, ``setup-token``) to the release's own
``bananawiki`` command, run as the service account with ``app.env`` loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import DEFAULT_ROOT, PRODUCT, caddy, caddy_blocks, profile
from .files import atomic_write, digest_file, maintenance_lock, read_environment, read_json, write_json
from .manager import PROXY_ROLLBACK, Manager

APP_COMMANDS = ("create-admin", "reset-password", "db", "jobs", "config", "setup-token", "hosting-admin")
FAILED_OUTCOMES = {"failed", "paused", "rolled_back"}


def source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="HTTPS or SSH source URL, including a private repository or fork")
    parser.add_argument("--branch")
    parser.add_argument("--fallback-branch", help="Use this branch only if the selected branch disappears")
    parser.add_argument("--clear-fallback", action="store_true")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--token-file", type=Path, help="Read an HTTPS token from a file and store it privately")
    auth.add_argument("--ssh-key", type=Path, help="Read an SSH deploy key from a file")
    auth.add_argument("--clear-credentials", action="store_true")
    parser.add_argument("--username", default="git", help="HTTPS repository username")
    parser.add_argument("--known-hosts", type=Path, help="Verified SSH host keys; required with --ssh-key")
    signatures = parser.add_mutually_exclusive_group()
    signatures.add_argument("--require-signatures", type=Path, metavar="ALLOWED_SIGNERS",
                            help="Deploy only revisions SSH-signed by a key in this allowed-signers file")
    signatures.add_argument("--clear-signatures", action="store_true", help="Stop requiring signed revisions")


def source_values(args: argparse.Namespace) -> dict[str, Any]:
    return {"url": args.repo, "branch": args.branch, "fallback": args.fallback_branch,
            "clear_fallback": args.clear_fallback, "token_file": args.token_file, "ssh_key": args.ssh_key,
            "known_hosts": args.known_hosts, "username": args.username,
            "clear_credentials": args.clear_credentials, "signers_file": args.require_signatures,
            "clear_signatures": args.clear_signatures}


def parser() -> argparse.ArgumentParser:
    from .backups.commands import add_commands

    result = argparse.ArgumentParser(prog="bananawiki", description=(
        f"{PRODUCT} server lifecycle. Automatic source updates stay disabled until explicitly enabled."))
    result.add_argument("--root", default=DEFAULT_ROOT, help="Managed installation directory")
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install a deployment mode, or restore one with --restore")
    install.add_argument("--mode", choices=profile.MODES)
    install.add_argument("--name", help="Service and installed command name")
    install.add_argument("--domain", help="Public HTTPS hostname; omit for a loopback-only installation")
    install.add_argument("--portal-domain", default="")
    install.add_argument("--port", type=int)
    install.add_argument("--restore", type=Path, metavar="PACKAGE")
    install.add_argument("--reuse-data", action="store_true",
                         help="Reuse saved configuration and data after uninstall or an interrupted installation")
    source_options(install)
    update = commands.add_parser("update", help="Back up, deploy, check readiness, and roll back on failure")
    update.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--allow-divergent", action="store_true",
                        help="Explicitly permit a reviewed switch to unrelated or rewritten history")
    update.add_argument("--retry-failed", action="store_true")
    backup = commands.add_parser("backup", aliases=["migrate"],
                                 help="Create a private portable package (code, data, configuration, credentials)")
    backup.add_argument("--output", type=Path)
    restore = commands.add_parser("restore", help="Back up the current data, then restore a portable package")
    restore.add_argument("package", type=Path)
    restore.add_argument("--domain")
    restore.add_argument("--port", type=int)
    rollback = commands.add_parser("rollback", help="Restore the package saved before the last successful update")
    rollback.add_argument("--package", type=Path)
    for name in ("status", "start", "stop", "restart", "recover"):
        commands.add_parser(name)
    updates = commands.add_parser("updates", help="Opt into or out of automatic source updates")
    updates.add_argument("action", choices=("status", "enable", "disable"))
    updates.add_argument("--interval", type=int, help="Check interval in minutes (5 to 10080)")
    updates.add_argument("--keep-backups", type=int, help="Automatic and pre-update packages to keep (2 to 30)")
    source_options(updates)
    source = commands.add_parser("source", help="Inspect or change the update source and its authentication")
    source.add_argument("action", choices=("show", "set", "check"))
    source_options(source)
    proxy = commands.add_parser("proxy", help="Print or install the matching HTTPS Caddy configuration")
    proxy.add_argument("--install", action="store_true")
    proxy.add_argument("--replace", action="store_true", help="Replace an existing Caddyfile, keeping a private backup")
    proxy.add_argument("--email", default="", help="ACME account e-mail for certificate notices")
    proxy.add_argument("--tls", choices=caddy.TLS_MODES,
                       help="Certificates: acme (default, per host), cloudflare-dns (wildcard through a Cloudflare "
                            "API token; needs Caddy with the caddy-dns/cloudflare module) or origin-cert (a "
                            "Cloudflare Origin CA certificate). Remembered for later updates.")
    proxy.add_argument("--cloudflare", action=argparse.BooleanOptionalAction, default=None,
                       help="The DNS records are proxied by Cloudflare (orange cloud): skip the ACME challenge "
                            "that cannot pass Cloudflare's proxy. Remembered for later updates.")
    proxy.add_argument("--cloudflare-token-file", type=Path, metavar="FILE",
                       help="Cloudflare API token (Zone:DNS:Edit) for --tls cloudflare-dns; stored root-only for Caddy")
    proxy.add_argument("--origin-cert", type=Path, metavar="PEM", help="Origin CA certificate for --tls origin-cert")
    proxy.add_argument("--origin-key", type=Path, metavar="PEM", help="Its private key")
    uninstall = commands.add_parser("uninstall", help="Remove services and updater; keep files unless --purge")
    uninstall.add_argument("--purge", action="store_true")
    uninstall.add_argument("--confirm", default="", metavar="SERVICE_NAME")
    agent = commands.add_parser("agent", help="Hosting runtime agent (run by its systemd unit)")
    agent.add_argument("action", choices=("serve", "status"))
    agent.add_argument("--socket", type=Path)
    add_commands(commands)
    for name in APP_COMMANDS:
        app = commands.add_parser(name, help="Application administration (runs as the service account)",
                                  add_help=False)
        app.add_argument("arguments", nargs=argparse.REMAINDER)
    return result


_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}")


def _proxy_options(manager: Manager, args: argparse.Namespace, record: dict[str, Any],
                   version: tuple[int, int, int] | None) -> tuple[caddy.ProxyOptions, dict[str, str]]:
    """The certificate mode for this run (arguments over ``proxy.json``) and Caddy secrets to install."""
    previous = caddy.options_for(record, version)
    mode = args.tls or previous.tls
    cloudflare = previous.cloudflare if args.cloudflare is None else args.cloudflare
    secrets: dict[str, str] = {}
    cert, key = (previous.origin_cert, previous.origin_key) if previous.tls == "origin-cert" else ("", "")
    if mode == "origin-cert":
        if bool(args.origin_cert) != bool(args.origin_key):
            raise ValueError("Give both --origin-cert and --origin-key.")
        if args.origin_cert:
            if args.install:
                cert, key = manager.system.install_origin_certificate(args.origin_cert, args.origin_key)
            else:
                cert, key = str(args.origin_cert.resolve()), str(args.origin_key.resolve())
        if not (cert and key):
            raise ValueError("--tls origin-cert needs --origin-cert and --origin-key (a Cloudflare Origin CA "
                             "certificate for the domain and *.domain).")
    if mode == "cloudflare-dns":
        if version is not None and version[:2] < caddy_blocks.WILDCARD_VERSION:
            raise ValueError("--tls cloudflare-dns needs Caddy 2.10 or newer (found "
                             + ".".join(map(str, version)) + ").")
        if args.install and caddy.CLOUDFLARE_DNS_MODULE not in manager.system.caddy_modules():
            raise ValueError("This Caddy has no Cloudflare DNS module. Install it with "
                             "'caddy add-package github.com/caddy-dns/cloudflare' (or a build from "
                             "https://caddyserver.com/download with caddy-dns/cloudflare), then run proxy again.")
        if args.cloudflare_token_file:
            token = args.cloudflare_token_file.read_text(encoding="utf-8").strip()
            if not _TOKEN.fullmatch(token):
                raise ValueError("The Cloudflare API token file must contain only the token.")
            secrets[caddy.CLOUDFLARE_TOKEN_VARIABLE] = token
        elif args.install and caddy.CLOUDFLARE_TOKEN_VARIABLE not in read_environment(
                manager.system.caddy_environment_file):
            raise ValueError("--tls cloudflare-dns needs --cloudflare-token-file (an API token with Zone:DNS:Edit "
                             "permission for the domain's zone).")
    elif args.cloudflare_token_file:
        raise ValueError("--cloudflare-token-file is only used with --tls cloudflare-dns.")
    options = caddy.ProxyOptions(tls=mode, cloudflare=cloudflare, origin_cert=cert, origin_key=key,
                                 client_ip=caddy_blocks.supports_client_ip(version))
    return options, secrets


def proxy_warnings(manager: Manager, settings: dict[str, Any]) -> list[str]:
    suffix = read_environment(manager.config_dir / "app.env").get("INSTANCE_URL_SUFFIX", "hosting")
    warnings = caddy.layout_warnings(settings, suffix)
    version = manager.system.caddy_version()
    if not caddy_blocks.supports_client_ip(version):
        warnings.append("Caddy " + ".".join(map(str, version or ())) + " cannot pass the visitors' real "
                        "addresses from Cloudflare (needs 2.7 or newer); rate limits then apply per Cloudflare "
                        "address. Upgrade Caddy and run proxy --install again.")
    return warnings


def configure_proxy(manager: Manager, args: argparse.Namespace) -> dict[str, Any] | None:
    settings = manager.settings()
    hosting = settings["mode"] == "hosting"
    routes = manager.system.routes_directory(settings) if hosting else None
    record = read_json(manager.config_dir / "proxy.json", {}) or {}
    version = manager.system.caddy_version()
    options, secrets = _proxy_options(manager, args, record, version)
    email = args.email or str(record.get("email") or "")
    text = caddy.render(settings, email=email, routes=str(routes) if routes else None, options=options)
    warnings = proxy_warnings(manager, settings)
    if not args.install:
        print(text, end="")
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return None
    if hosting:
        manager.system.ensure_routes_directory(settings)
    destination = manager.system.proxy_file
    previous = destination.read_bytes() if destination.exists() else None
    owned = destination.is_file() and record.get("installed_sha256") == digest_file(destination)
    if previous and not owned and not args.replace:
        raise ValueError("Caddy already has a configuration. Merge the output of proxy into it, or use "
                         "proxy --install --replace for an intentional replacement.")
    candidate = manager.config_dir / "Caddyfile.next"
    atomic_write(candidate, text)
    backup = record.get("previous") if owned else None
    try:
        manager.system.validate_proxy(candidate, secrets)
        if secrets:
            # {env.*} placeholders are read by the running server: restart, not reload.
            manager.system.install_caddy_environment({**read_environment(manager.system.caddy_environment_file),
                                                      **secrets})
        if previous and not owned:
            backup = str(manager.root / "backups" / ("Caddyfile-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")))
            atomic_write(backup, previous)
        atomic_write(destination, text, 0o644)
        action = "restart" if secrets else "reload-or-restart"
        try:
            manager.system.run(["systemctl", action, "caddy"])
        except BaseException:
            if previous is not None:
                atomic_write(destination, previous, 0o644)
                manager.system.run(["systemctl", "reload-or-restart", "caddy"], check=False)
            else:
                destination.unlink(missing_ok=True)
            raise
    finally:
        candidate.unlink(missing_ok=True)
    write_json(manager.config_dir / "proxy.json", {"installed_sha256": digest_file(destination), "previous": backup,
                                                   "email": email, "tls": options.record()})
    result: dict[str, Any] = {"outcome": "proxy configured", "domain": settings["domain"], "tls": options.tls,
                              "cloudflare": options.behind_cloudflare}
    if warnings:
        result["warnings"] = warnings
    return result


def lifecycle(manager: Manager, args: argparse.Namespace) -> dict[str, Any] | None:
    """Start, stop, restart or recover the remembered set of services (restart also converges units)."""
    with maintenance_lock(manager.root):
        recovered = manager.recover()
        settings = manager.settings()
        services = manager.services(settings)
        names = [service.name for service in services]
        containers = manager.system.containers(settings)
        if args.command in {"stop", "restart"}:
            manager.system.stop(names)
            manager.system.stop_containers(containers)
        undo: dict[str, Any] = {}
        details: dict[str, Any] = {}
        try:
            if args.command == "restart":
                manager.write_runtime(settings)
                manager.install_units(settings)
                details = manager.refresh_proxy(settings, undo)
            if args.command in {"start", "restart"}:
                manager.system.start(names)
                if not manager.system.healthy(settings, services, containers):
                    raise RuntimeError("The service failed readiness checks. Inspect its journal before allowing "
                                       "traffic.")
                (manager.root / "data/.banana-maintenance").unlink(missing_ok=True)
        except BaseException:
            manager.restore_proxy(undo)
            raise
        (manager.config_dir / PROXY_ROLLBACK).unlink(missing_ok=True)
        return manager.event(args.command, "complete", recovered=recovered, **details)


def run_app_command(manager: Manager, command: str, arguments: list[str]) -> int:
    """Run ``bananawiki <command> …`` from the current release as the service account."""
    settings = manager.settings()
    hosting_command = command == "hosting-admin"
    if (settings["mode"] == "hosting") != hosting_command:
        raise ValueError("hosting-admin is for hosting installations; the other application commands are for wikis.")
    current = manager.root / "current"
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(manager.root / "data"),
                   **read_environment(manager.config_dir / "app.env")}
    module = ["bananawiki.hosting.admin"] if hosting_command else ["bananawiki.cli", command]
    argv = ["runuser", "-u", settings["service"], "--", str(current / ".venv/bin/python"), "-m", *module,
            *arguments]
    return subprocess.run(argv, cwd=current, env=environment, check=False).returncode


def backups(manager: Manager, args: argparse.Namespace) -> dict[str, Any]:
    from .backups.commands import handle
    from .backups.store import Store

    def schedule(policy: dict[str, Any]) -> None:
        if (manager.config_dir / "installation.json").exists():
            manager.system.install_backup_timer(manager.settings(), policy)
        elif policy["enabled"]:
            raise ValueError("Install or restore the application before enabling its backup schedule.")

    return handle(args, Store(manager.config_dir / "remote-backup", PRODUCT),
                  create_package=lambda: manager.backup(manager.backup_name("remote")),
                  restore_package=lambda package, options: manager.restore(
                      package, name=options.name, domain=options.domain, port=options.port),
                  schedule=schedule)


def dispatch(manager: Manager, args: argparse.Namespace) -> dict[str, Any] | None:
    command = args.command
    if command == "install":
        if args.restore:
            return manager.restore(args.restore, new=True, name=args.name, domain=args.domain, port=args.port)
        if not args.mode:
            raise ValueError("Choose --mode, or use --restore PACKAGE to recover a saved deployment.")
        result = manager.install(Path(__file__).resolve().parents[2], mode=args.mode, name=args.name,
                                 domain=args.domain or "", portal_domain=args.portal_domain, port=args.port,
                                 source_options=source_values(args), reuse_data=args.reuse_data)
        warnings = caddy.layout_warnings(manager.settings())
        return {**result, "warnings": warnings} if warnings and isinstance(result, dict) else result
    if command == "update":
        return manager.update(automatic=args.automatic, allow_divergent=args.allow_divergent,
                              retry_failed=args.retry_failed)
    if command in {"backup", "migrate"}:
        return {"package": str(manager.backup(args.output)), "contains_secrets": True}
    if command == "backups":
        return backups(manager, args)
    if command == "restore":
        return manager.restore(args.package, domain=args.domain, port=args.port)
    if command == "rollback":
        archive = args.package or (read_json(manager.config_dir / "last-update.json", {}) or {}).get("backup")
        if not archive:
            raise ValueError("No previous update package is recorded. Use rollback --package PATH.")
        return manager.restore(Path(archive))
    if command == "status":
        return manager.status()
    if command == "updates":
        if args.action == "status":
            return {"source": manager.source(), "updates": manager.policy()}
        changes = source_values(args)
        if any(value for key, value in changes.items() if key != "username"):
            with maintenance_lock(manager.root):
                manager.configure_source(**changes)
        result = manager.set_updates(args.action == "enable", interval=args.interval, keep=args.keep_backups)
        print("Automatic updates are now enabled. Changes on the configured branch can alter or remove features."
              if args.action == "enable" else
              "Automatic updates are disabled. An update already applying its changes will finish safely.")
        return result
    if command == "source":
        if args.action == "set":
            with maintenance_lock(manager.root):
                return manager.configure_source(**source_values(args))
        if args.action == "check":
            from .source import GitSource

            with maintenance_lock(manager.root):
                revision, branch, forward = GitSource(manager.root, manager.source()).resolve(
                    manager.settings()["revision"])
            return {"revision": revision, "selected_branch": branch, "fast_forward": forward, "deployed": False}
        return manager.source()
    if command in {"start", "stop", "restart", "recover"}:
        return lifecycle(manager, args)
    if command == "proxy":
        with maintenance_lock(manager.root):
            return configure_proxy(manager, args)
    if command == "uninstall":
        return manager.uninstall(purge=args.purge, confirm=args.confirm)
    if command == "agent":
        settings = manager.settings()
        path = args.socket or Path(profile.agent_socket(settings))
        if args.action == "serve":
            from .runtime_agent import serve

            raise SystemExit(serve(manager.root, path))
        from .agent_client import AgentClient

        return AgentClient(path, timeout=10).call("ping")
    raise ValueError("Unsupported command.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print("Server maintenance needs root. Run this command with sudo.", file=sys.stderr)
        return 1
    manager = Manager(args.root)
    try:
        if args.command in APP_COMMANDS:
            return run_app_command(manager, args.command, args.arguments)
        result = dispatch(manager, args)
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        if args.command == "install":
            name = manager.settings()["service"]
            print(f"Next: review {manager.config_dir / 'app.env'}. Use '{name} proxy' for the HTTPS configuration "
                  f"and '{name} updates enable' only if you want automatic updates.")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        return 2 if outcome in FAILED_OUTCOMES else 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"{PRODUCT}: {error}", file=sys.stderr)
        if manager.config_dir.exists():
            manager.event(args.command, "failed", reason=str(error))
        return 1
