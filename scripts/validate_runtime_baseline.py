#!/usr/bin/env python3
"""Validate BananaWiki production runtime baseline against hardened defaults."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _load_module(path: Path, module_name: str):
    # Ensure local config modules (config.py / hosting config) can be imported.
    for candidate in (str(path.resolve().parent), str(path.resolve().parents[1])):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    loader.exec_module(module)
    return module


def _contains(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    return needle in path.read_text(encoding="utf-8", errors="replace")


def _validate_gunicorn_conf(path: Path, hosting: bool = False) -> List[CheckResult]:
    results: List[CheckResult] = []
    try:
        mod = _load_module(path, f"gunicorn_conf_{'hosting' if hosting else 'wiki'}")
    except Exception as exc:
        return [CheckResult(f"{path.name}:load", False, f"failed to import ({exc})")]

    expected_timeout = 120 if hosting else 60
    actual_timeout = int(getattr(mod, "timeout", -1))
    results.append(
        CheckResult(
            f"{path.name}:timeout",
            actual_timeout >= expected_timeout,
            f"timeout={actual_timeout}, expected>={expected_timeout}",
        )
    )
    results.append(
        CheckResult(
            f"{path.name}:worker_class",
            getattr(mod, "worker_class", "") == "gthread",
            f"worker_class={getattr(mod, 'worker_class', None)}",
        )
    )
    results.append(
        CheckResult(
            f"{path.name}:preload_app",
            bool(getattr(mod, "preload_app", False)),
            f"preload_app={getattr(mod, 'preload_app', None)}",
        )
    )
    text = path.read_text(encoding="utf-8", errors="replace")
    results.append(
        CheckResult(
            f"{path.name}:worker_tmp_dir",
            "_resolve_worker_tmp_dir" in text and "worker_tmp_dir" in text and "/dev/shm" in text,
            "expects /dev/shm worker heartbeat handling",
        )
    )
    return results


def _validate_service_unit(path: Path) -> List[CheckResult]:
    if not path.is_file():
        return [CheckResult(f"{path.name}:exists", False, "service file missing")]
    import configparser
    parsed = configparser.ConfigParser(interpolation=None, strict=False)
    parsed.read(path)
    service = parsed["Service"] if parsed.has_section("Service") else {}
    return [
        CheckResult(f"{path.name}:service_account", service.get("User", "root") != "root",
                    "expects a dedicated unprivileged application account"),
        CheckResult(f"{path.name}:restart_policy", service.get("Restart") in {"always", "on-failure"},
                    "expects supervised restarts"),
        CheckResult(f"{path.name}:protect_system", service.get("ProtectSystem") == "strict",
                    "expects a read-only application filesystem"),
        CheckResult(f"{path.name}:private_permissions", service.get("UMask") == "0077",
                    "expects private runtime file permissions"),
    ]


def _validate_nginx_conf(path: Path) -> List[CheckResult]:
    if not path.exists():
        return [CheckResult(f"{path.name}:exists", False, "file missing")]
    text = path.read_text(encoding="utf-8", errors="replace")
    hosting_180 = bool(re.search(r"proxy_read_timeout\s+180s;", text))
    return [
        CheckResult(
            f"{path.name}:hosting_timeout_180",
            hosting_180,
            "expects 180s read timeout for hosting/wildcard blocks",
        )
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate BananaWiki production baseline hardening")
    parser.add_argument("--repo-root", default=".", help="Repository root path")
    parser.add_argument(
        "--nginx-conf",
        default="",
        help="Optional existing nginx configuration to validate",
    )
    parser.add_argument("--unit-file", action="append", default=[], help="Optional installed systemd unit to inspect (repeatable)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.repo_root).resolve()

    checks: List[CheckResult] = []
    checks.extend(_validate_gunicorn_conf(root / "gunicorn.conf.py", hosting=False))
    checks.extend(_validate_gunicorn_conf(root / "hosting" / "gunicorn.conf.py", hosting=True))
    for unit in args.unit_file:
        checks.extend(_validate_service_unit(Path(unit)))

    if args.nginx_conf:
        checks.extend(_validate_nginx_conf(Path(args.nginx_conf).resolve()))

    failed = [c for c in checks if not c.ok]
    for check in checks:
        prefix = "PASS" if check.ok else "FAIL"
        print(f"[{prefix}] {check.name}: {check.detail}")

    if failed:
        print(f"\nBaseline validation failed: {len(failed)} check(s) did not match hardened expectations.")
        return 1
    print("\nBaseline validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
