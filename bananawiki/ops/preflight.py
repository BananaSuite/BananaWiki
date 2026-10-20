"""``banana preflight``: whether this release can run on this server, checked before an update stops anything.

An update (by a 1.7 or later controller) runs the *new* release's own copy of this command
once it has prepared that release and before it stops anything, because the
installed controller cannot know what a newer release refuses once it runs:
storage without the quotas it needs, a setting it no longer accepts. It runs
as root with the system Python and changes nothing. Its answer is one JSON
line: exit 0 with ``{"ok": true, "protocol": 1, "warnings": [...], "notes":
[...]}`` when this release can run here (``notes``: what an operator should
know about a supported configuration, such as wiki storage without hard
quotas; a controller that does not know the key ignores it), exit 3 with
``{"ok": false, "protocol": 1, "reason":
...}`` when it cannot (the update then stops nothing and records the revision
as failed, so automatic updates do not try it again every hour). A refusal
caused by the host for now (Docker did not answer) adds ``"retryable": true``:
the update stops nothing and records nothing, so the next run asks again.
Operators can run it too: ``sudo /path/to/checkout/banana --root /opt/bananawiki preflight``.

Keep this protocol stable: controllers of earlier releases run it in newer ones.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .files import Refused, read_environment
from .manager import Manager
from .profile import ReleaseFeatures
from .system import DockerUnavailable

PROTOCOL = 1
REFUSED = 3
IPV6_SYSCTLS = Path("/proc/sys/net/ipv6")  # missing when the kernel was booted with ipv6.disable=1
_IP_ADDRESS = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"  # as hosting.config
                         r"|^\[?[0-9a-fA-F:]+\]?$")


def hosting_mode(environment: Mapping[str, str]) -> str:
    """The portal's hosting mode for *environment* (``config/app.env``), decided as ``bananawiki.hosting.config``
    decides it: that module needs the release's own dependencies, and this check runs with the system Python."""
    explicit = (environment.get("HOSTING_MODE") or "").strip().lower()
    if explicit:
        return explicit
    base = (environment.get("BASE_DOMAIN") or "").strip().lower()
    if not base or _IP_ADDRESS.match(base) or base in ("localhost", "0.0.0.0"):  # noqa: S104 - a value, not a bind
        return "port"
    return "subdomain" if "." in base else "port"


def check_hosting_host(environment: Mapping[str, str]) -> None:
    """What would keep this release's portal or every wiki from starting on this host (ValueError)."""
    if ((environment.get("HOSTING_ALLOW_TENANT_PLUGINS") or "").strip().lower() in {"1", "true", "yes", "on"}
            and (mode := hosting_mode(environment)) in ("port", "onion")):
        raise ValueError(f"HOSTING_ALLOW_TENANT_PLUGINS=1 requires subdomain hosting, and this platform runs in {mode} "
                         "mode: this release's portal would refuse to start. Set HOSTING_ALLOW_TENANT_PLUGINS=0 in "
                         "config/app.env (or configure a domain for subdomain mode), then retry.")
    if not IPV6_SYSCTLS.is_dir():
        raise ValueError("This host's kernel has no IPv6 (it was booted with ipv6.disable=1), and every wiki "
                         "container of this release switches IPv6 off with sysctls Docker cannot set then: no wiki "
                         "would start. Remove ipv6.disable=1 from the kernel command line and reboot (to keep IPv6 "
                         "off on the host, set the sysctl net.ipv6.conf.all.disable_ipv6=1 instead), then retry.")


def check(manager: Manager, release: Path | None = None) -> dict[str, Any]:
    """This release's verdict on the server *manager* manages; raises (:class:`Refused`, ValueError) to refuse."""
    features = ReleaseFeatures.of(release or Path(__file__).resolve().parents[2])
    settings = manager.settings()
    manager.package_limit()
    warnings: list[str] = []
    notes: list[str] = []
    if settings["mode"] == "hosting":
        manager.unready_policy()
        check_hosting_host(read_environment(manager.config_dir / "app.env"))
        manager.system.check_docker(settings)
        manager.system.tenant_quota_setting(settings)
        if features.project_quotas:
            # The wikis running now must start again under this release's runtime agent.
            policy = (manager.system.check_tenant_storage(settings, required=not features.quota_modes)
                      or manager.system.tenant_storage(settings,
                                                       configured=None if features.quota_modes else "required"))
            message = policy.warning()
            if message:  # storage that never had hard quotas, or off, is supported: a note
                (warnings if policy.severity in ("warning", "error") else notes).append(message)
    return {"ok": True, "protocol": PROTOCOL, "warnings": warnings, "notes": notes}


def run(manager: Manager) -> int:
    """Print the verdict as one JSON line; 0 when this release can run here, :data:`REFUSED` otherwise."""
    try:
        answer, code = check(manager), 0
    except DockerUnavailable as error:  # the host, for now: the controller asks again at its next run
        answer, code = {"ok": False, "protocol": PROTOCOL, "retryable": True, "reason": str(error)}, REFUSED
    except (Refused, ValueError) as error:
        answer, code = {"ok": False, "protocol": PROTOCOL, "reason": str(error)}, REFUSED
    except Exception as error:  # noqa: BLE001 - a release that cannot check this server must not be deployed on it
        answer, code = {"ok": False, "protocol": PROTOCOL,
                        "reason": f"The preflight check failed: {type(error).__name__}: {error}"}, REFUSED
    print(json.dumps(answer), flush=True)
    return code
