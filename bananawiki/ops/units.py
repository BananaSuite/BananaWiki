"""systemd unit files and the installed command wrapper (pure text rendering).

Every file starts with the ``# Managed by BananaSuite`` marker 1.4 used, so
either version recognises the other's files as its own and refuses to touch
units it did not write. Units for 1.6 releases also carry
``# bananawiki-ops: 2``; its absence tells the controller that a release was
installed by the 1.4 updater and its units still need converging.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import MANAGED_MARKER, PRODUCT, UNIT_GENERATION
from .profile import ReleaseFeatures, Service

# Sandbox for the unprivileged application services. The application only
# writes below data/ (C7); /dev/shm stays writable for Gunicorn heartbeats.
_APP_SANDBOX = (
    "NoNewPrivileges=true\n"
    "PrivateTmp=true\n"
    "PrivateDevices=true\n"
    "ProtectSystem=strict\n"
    "ProtectHome=true\n"
    "ProtectKernelTunables=true\n"
    "ProtectKernelModules=true\n"
    "ProtectKernelLogs=true\n"
    "ProtectControlGroups=true\n"
    "ProtectClock=true\n"
    "ProtectHostname=true\n"
    "ProtectProc=invisible\n"
    "RestrictNamespaces=true\n"
    "RestrictRealtime=true\n"
    "RestrictSUIDSGID=true\n"
    "LockPersonality=true\n"
    "CapabilityBoundingSet=\n"
    "AmbientCapabilities=\n"
    "SystemCallArchitectures=native\n"
    "SystemCallFilter=@system-service\n"
    "SystemCallErrorNumber=EPERM\n"
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n"
)

# The runtime agent runs as root (it drives the Docker socket) but needs no
# network, no writable files outside its runtime directory and only enough
# privilege to look into the service-owned tenant directories.
_AGENT_SANDBOX = (
    "NoNewPrivileges=true\n"
    "PrivateTmp=true\n"
    "PrivateNetwork=true\n"
    "ProtectSystem=strict\n"
    "ProtectHome=true\n"
    "ProtectKernelTunables=true\n"
    "ProtectKernelModules=true\n"
    "ProtectKernelLogs=true\n"
    "ProtectControlGroups=true\n"
    "ProtectClock=true\n"
    "ProtectHostname=true\n"
    "RestrictNamespaces=true\n"
    "RestrictRealtime=true\n"
    "RestrictSUIDSGID=true\n"
    "LockPersonality=true\n"
    "CapabilityBoundingSet=CAP_DAC_READ_SEARCH\n"
    "AmbientCapabilities=\n"
    "SystemCallArchitectures=native\n"
    "SystemCallFilter=@system-service\n"
    "SystemCallErrorNumber=EPERM\n"
    "RestrictAddressFamilies=AF_UNIX\n"
)


def _exec(command: list[str]) -> str:
    return " ".join(json.dumps(str(argument)) for argument in command)


def service_unit(settings: dict[str, Any], service: Service, features: ReleaseFeatures) -> str:
    if not features.hardened:
        return legacy_service_unit(settings, service)
    root, name = Path(settings["root"]), settings["service"]
    after = ["network-online.target", *service.after]
    wants = ["network-online.target", *service.after]
    if service.privileged:
        after.append("docker.service")
        wants.append("docker.service")
        identity = f"User=root\nGroup={name}\n"
        # The routes directory is read by Caddy (its own user), hence 0755.
        runtime = (
            f"RuntimeDirectory={service.name}\nRuntimeDirectoryMode=0750\n"
            f"StateDirectory={name}-routes\nStateDirectoryMode=0755\n"
            f"Environment=HOME=/run/{service.name} DOCKER_CONFIG=/run/{service.name}/docker\n"
        )
        sandbox = _AGENT_SANDBOX
        writable = ""
    else:
        identity = f"User={name}\nGroup={name}\n"
        runtime = f"Environment=HOME={root / 'data'}\n"
        sandbox = _APP_SANDBOX
        writable = f"ReadWritePaths={root / 'data'}\n"
    return (
        f"{MANAGED_MARKER}\n{UNIT_GENERATION}\n[Unit]\nDescription={service.description} ({name})\n"
        f"After={' '.join(after)}\nWants={' '.join(wants)}\n\n[Service]\nType=simple\n{identity}"
        f"WorkingDirectory={root / 'current'}\nEnvironmentFile={root / 'config/app.env'}\n{runtime}"
        f"ExecStart={_exec(service.command)}\n"
        "Restart=always\nRestartSec=5\nTimeoutStartSec=180\nTimeoutStopSec=180\nKillMode=mixed\nUMask=0077\n"
        f"{sandbox}{writable}\n[Install]\nWantedBy=multi-user.target\n"
    )


def legacy_service_unit(settings: dict[str, Any], service: Service) -> str:
    """Byte-for-byte the unit 1.4 wrote, for releases that predate the 1.6 sandbox."""
    root = Path(settings["root"])
    after, extra = "network-online.target", ""
    if settings["mode"] == "hosting":
        after += " docker.service"
        extra = "SupplementaryGroups=docker\n"
    return (
        f"{MANAGED_MARKER}\n[Unit]\nDescription={PRODUCT} {settings['mode']}\n"
        f"After={after}\nWants=network-online.target\n\n[Service]\nUser={settings['service']}\n"
        f"Group={settings['service']}\nWorkingDirectory={root / 'current'}\n"
        f"EnvironmentFile={root / 'config/app.env'}\nExecStart={_exec(service.command)}\n"
        "Restart=always\nRestartSec=5\nTimeoutStartSec=180\nTimeoutStopSec=180\nKillMode=mixed\nUMask=0077\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
        f"ReadWritePaths={root / 'data'}\n{extra}\n[Install]\nWantedBy=multi-user.target\n"
    )


def wrapper(settings: dict[str, Any]) -> str:
    """``/usr/local/bin/<service>``: runs the current release's controller with the system Python."""
    root = Path(settings["root"])
    return f'#!/bin/sh\n{MANAGED_MARKER}\nexec /usr/bin/python3 {root / "current/banana"} --root {root} "$@"\n'


def _oneshot(settings: dict[str, Any], description: str, arguments: str, timeout: int) -> str:
    root = Path(settings["root"])
    return (
        f"{MANAGED_MARKER}\n[Unit]\nDescription={description}\n"
        "After=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nUMask=0077\n"
        f"WorkingDirectory={root / 'current'}\n"
        f"ExecStart=/usr/bin/python3 {root / 'current/banana'} --root {root} {arguments}\n"
        f"TimeoutStartSec={timeout}\nPrivateTmp=true\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\n"
    )


def _timer(description: str, interval: int, boot_delay: str, persistent: bool) -> str:
    return (
        f"{MANAGED_MARKER}\n[Unit]\nDescription={description}\n\n[Timer]\n"
        f"OnBootSec={boot_delay}\nOnUnitActiveSec={int(interval)}min\nRandomizedDelaySec=120\n"
        + ("Persistent=true\n" if persistent else "")
        + "\n[Install]\nWantedBy=timers.target\n"
    )


def update_units(settings: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    return (
        _oneshot(settings, f"Optional {PRODUCT} source update", "update --automatic", 3600),
        _timer(f"Optional {PRODUCT} update schedule", policy["interval_minutes"], "5min", True),
    )


def backup_units(settings: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    return (
        _oneshot(settings, f"Optional encrypted {PRODUCT} backup", "backups run --automatic", 7200),
        _timer(f"Optional {PRODUCT} backup schedule", policy["interval_minutes"], "15min", False),
    )


def is_managed(text: str, root: Path) -> bool:
    """A unit or wrapper this root owns (written by 1.4 or 1.6 for the same root)."""
    return MANAGED_MARKER in text and (
        f"WorkingDirectory={root / 'current'}\n" in text or str(root / "current/banana") in text
    )


def is_current_generation(text: str) -> bool:
    return UNIT_GENERATION in text


def examples() -> dict[str, str]:
    """The ``deploy/systemd/<mode>/*`` examples: what ``install`` writes for /opt/bananawiki."""
    from .profile import services

    features = ReleaseFeatures(runtime_agent=True, hardened=True)
    output = {}
    for mode, port in (("wiki", 5001), ("hosting", 5099)):
        settings = {"root": "/opt/bananawiki", "service": "bananawiki", "mode": mode, "port": port}
        for service in services(settings, features):
            output[f"{mode}/{service.name}.service"] = service_unit(settings, service, features)
    policy = {"interval_minutes": 60}
    settings = {"root": "/opt/bananawiki", "service": "bananawiki"}
    output["bananawiki-update.service"], output["bananawiki-update.timer"] = update_units(settings, policy)
    return output
