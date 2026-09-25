#!/usr/bin/env python3
"""Fail-closed public-beta readiness gate for BananaWiki Hosting."""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument(
        "--external-verified", action="store_true",
        help="confirm the operator completed the Cloudflare, firewall, alerting and restore checks",
    )
    args = parser.parse_args()
    failures = []
    warnings = []

    from hosting import config

    def require(condition, message):
        if not condition:
            failures.append(message)

    require(config.HOSTING_INSTANCE_RUNTIME == "docker",
            "HOSTING_INSTANCE_RUNTIME must be docker for public custom plugins")
    require(config.HOSTING_HOST in {"127.0.0.1", "::1", "localhost"},
            "hosting origin must bind to loopback, not a public interface")
    require(bool(config.HOSTING_PROXY_MODE), "trusted reverse-proxy mode must be enabled")
    require(bool(config.HOSTING_BOOTSTRAP_TOKEN),
            "HOSTING_BOOTSTRAP_TOKEN must protect first-admin signup")
    require(bool(config.BASE_DOMAIN), "BASE_DOMAIN must be configured")
    require(shutil.which("docker") is not None, "Docker CLI is not installed")

    if shutil.which("docker"):
        image = config.HOSTING_CONTAINER_IMAGE
        inspected = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        require(inspected.returncode == 0, f"tenant image is missing: {image}")

    db_path = Path(config.HOSTING_DATABASE_PATH)
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT signup_mode FROM hosting_settings WHERE id=1"
            ).fetchone()
            mode = row[0] if row else "open"
            require(mode in {"approval", "invite", "closed"},
                    "public beta signup mode must be approval, invite, or closed")
            admins = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE is_admin=1 AND deleted_at IS NULL"
            ).fetchone()[0]
            if admins:
                mfa_admins = conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE is_admin=1 "
                    "AND deleted_at IS NULL AND totp_enabled=1"
                ).fetchone()[0]
                require(mfa_admins == admins, "every active platform admin must enable MFA")
        except sqlite3.Error as exc:
            failures.append(f"hosting database readiness query failed: {exc}")
        finally:
            conn.close()
    else:
        warnings.append("hosting database does not exist yet; rerun after bootstrap")

    if os.name == "posix" and shutil.which("systemctl"):
        for unit in ("bananawiki-hosting", "bananawiki-hosting-maintenance"):
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                warnings.append(f"systemd unit is not active: {unit}")

    if not args.external_verified:
        warnings.append(
            "verify Cloudflare Tunnel or Full (strict), origin firewall rules, "
            "DNS, alerts, and an off-host restore drill externally; then rerun "
            "with --external-verified"
        )
    for message in failures:
        print(f"FAIL  {message}")
    for message in warnings:
        print(f"WARN  {message}")
    if failures or (args.strict and warnings):
        print("NOT READY")
        return 1
    print("READY (local gates passed; external controls still require verification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
