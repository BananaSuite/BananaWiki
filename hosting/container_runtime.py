"""Docker isolation boundary for managed BananaWiki tenants.

The hosting portal remains on the host.  Each wiki runs from an immutable
image with only its own data directory mounted writable. Each tenant gets
its own internal bridge. The host operator can explicitly enable outbound
network access for integrations that need Internet or private LAN services.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess

from . import config

logger = logging.getLogger("hosting.container_runtime")

# Set once we have warned about outbound tenant networking so a host with many
# tenants does not repeat the warning on every start.
_OUTBOUND_WARNING_EMITTED = False


def warn_if_tenant_network_outbound():
    """Log one warning when tenant containers get outbound network access.

    Port and onion hosting keep tenants on a routed bridge, because Docker
    does not publish ports from an internal network and those modes reach
    tenants through published ports.  Tenant plugin code can then reach the
    Internet, the host's LAN and any host service listening on the bridge
    gateway, and in onion mode a clearnet request can reveal the server's
    address.  The portal calls this at startup (recover_running_instances)
    and before starting a container.  Returns True when it warned.
    """
    global _OUTBOUND_WARNING_EMITTED
    if _OUTBOUND_WARNING_EMITTED or not config.HOSTING_TENANT_NETWORK_OUTBOUND:
        return False
    _OUTBOUND_WARNING_EMITTED = True
    logger.warning(
        "Tenant containers have outbound network access (HOSTING_MODE=%s, "
        "HOSTING_TENANT_NETWORK=outbound). Tenant plugin code can reach the "
        "Internet, the host LAN and host services bound to the bridge gateway. "
        "Add host firewall rules in the DOCKER-USER chain that stop tenant "
        "bridges from reaching the host, private address ranges and cloud "
        "metadata addresses; see docs/deployment.md.",
        config.HOSTING_MODE,
    )
    return True


_PATH_ENV_KEYS = frozenset({
    "BW_DATABASE_PATH",
    "BW_INSTANCE_DIR",
    "BW_MAINTENANCE_FILE",
    "BW_UPLOAD_FOLDER",
    "BW_ATTACHMENT_FOLDER",
    "BW_CHAT_ATTACHMENT_FOLDER",
    "BW_KANBAN_ATTACHMENT_FOLDER",
    "BW_CUSTOM_PAGE_FILES_FOLDER",
    "BW_LOG_FILE",
    "BW_SITE_EXPORT_TEMP_DIR",
})


def _safe_slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return (value[:28] or "tenant")


def container_name(data_dir):
    real = os.path.realpath(data_dir)
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:12]
    return f"bananawiki-{_safe_slug(os.path.basename(real))}-{digest}"


def network_name(data_dir):
    return f"{container_name(data_dir)}-net"


def _docker(*args, timeout=30, check=False):
    return subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=check,
    )


def docker_available():
    try:
        return _docker("version", "--format", "{{.Server.Version}}", timeout=5).returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _ensure_private_network(data_dir):
    name = network_name(data_dir)
    internal = config.HOSTING_TENANT_NETWORK == "isolated"
    inspected = _docker("network", "inspect", name, timeout=10)
    if inspected.returncode == 0:
        result = _docker("network", "inspect", "--format", "{{.Internal}}", name, timeout=10, check=True)
        if result.stdout.strip().lower() != str(internal).lower():
            # start_container already removed this tenant's previous container.
            # Never retain an older, more permissive network after a failure.
            _docker("network", "rm", name, timeout=20, check=True)
            inspected = _docker("network", "inspect", name, timeout=10)
    if inspected.returncode != 0:
        _docker(
            "network", "create", "--driver", "bridge", *(["--internal"] if internal else []), name,
            timeout=20, check=True,
        )
    return name


def _container_env(host_env, data_dir, internal_port):
    real = os.path.realpath(data_dir)
    result = {}
    for key, value in host_env.items():
        if not key.startswith("BW_") or key in {"BW_SETUP_TOKEN", "BW_SECRET_KEY"}:
            continue
        value = str(value)
        if key in _PATH_ENV_KEYS:
            value_real = os.path.realpath(value)
            if value_real == real:
                value = "/data"
            elif value_real.startswith(real + os.sep):
                value = "/data/" + os.path.relpath(value_real, real)
        result[key] = value
    # External plugins are allowed inside the container boundary by design,
    # except while the operator's plugin quarantine is in force: the portal
    # sets BW_MANAGED_PLUGIN_QUARANTINE=1 in host_env and we keep external
    # plugins disabled so a plugin that already ran cannot survive the kill
    # switch by re-enabling itself in the tenant database.
    quarantined = host_env.get("BW_MANAGED_PLUGIN_QUARANTINE") == "1"
    result.update({
        "BW_PORT": str(internal_port),
        "BW_HOST": "0.0.0.0",
        "BW_PROXY_MODE": "1",
        "BW_MANAGED_HOSTING": "1",
        "BW_PLUGIN_ISOLATION": "container",
        "BW_ALLOW_EXTERNAL_PLUGINS": "0" if quarantined else "1",
        "BW_EXTERNAL_PLUGINS_DIR": "/data/external_plugins",
    })
    return result


def build_run_command(*, data_dir, host_port, host_env, image, internal_port,
                      memory_mb, nofile_limit, cpu_limit, pids_limit,
                      uid=None, gid=None, network=None):
    """Return the hardened ``docker run`` argv used for one tenant."""
    real = os.path.realpath(data_dir)
    uid = os.stat(real).st_uid if uid is None else int(uid)
    gid = os.stat(real).st_gid if gid is None else int(gid)
    network = network or network_name(real)
    env = _container_env(host_env, real, internal_port)
    cmd = [
        "docker", "run", "--detach", "--name", container_name(real),
        "--network", network,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(max(32, int(pids_limit))),
        "--memory", f"{max(128, int(memory_mb))}m",
        "--memory-swap", f"{max(128, int(memory_mb))}m",
        "--cpus", str(cpu_limit),
        "--ulimit", f"nofile={max(128, int(nofile_limit))}:{max(128, int(nofile_limit))}",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
         "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=1777",
        "--user", f"{uid}:{gid}",
         "--mount", f"type=bind,src={real},dst=/data",
        "--publish", f"127.0.0.1:{int(host_port)}:{int(internal_port)}",
        "--label", "org.bananawiki.role=tenant",
        "--label", f"org.bananawiki.data-dir={real}",
        "--label", f"org.bananawiki.internal-port={int(internal_port)}",
    ]
    for key in sorted(env):
        cmd.extend(["--env", f"{key}={env[key]}"])
    cmd.append(image)
    return cmd


def start_container(*, data_dir, host_port, host_env, image, internal_port,
                    memory_mb, nofile_limit, cpu_limit, pids_limit):
    if not docker_available():
        raise RuntimeError("Docker is required for the configured tenant runtime")
    warn_if_tenant_network_outbound()
    os.makedirs(os.path.join(data_dir, "external_plugins"), exist_ok=True)
    name = container_name(data_dir)
    _docker("rm", "--force", name, timeout=20)
    network = _ensure_private_network(data_dir)
    cmd = build_run_command(
        data_dir=data_dir,
        host_port=host_port,
        host_env=host_env,
        image=image,
        internal_port=internal_port,
        memory_mb=memory_mb,
        nofile_limit=nofile_limit,
        cpu_limit=cpu_limit,
        pids_limit=pids_limit,
        network=network,
    )
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Docker failed to start tenant")
    return container_pid(data_dir)


def container_ip(data_dir):
    """Return the container's IP address on its private bridge network.

    The host can always reach a container directly on its own bridge IP.
    Using the bridge IP avoids relying on Docker's iptables port-publishing
    rules, which may not be present on all Docker/kernel combinations.

    Returns ``None`` if the container is not running or has no IP yet.
    """
    try:
        result = _docker(
            "inspect",
            "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container_name(data_dir),
            timeout=10,
        )
        if result.returncode != 0:
            return None
        ip = result.stdout.strip()
        return ip if ip else None
    except (ValueError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def container_pid(data_dir):
    try:
        result = _docker(
            "inspect", "--format", "{{.State.Pid}}", container_name(data_dir),
            timeout=10,
        )
        if result.returncode != 0:
            return None
        pid = int(result.stdout.strip())
        return pid if pid > 0 else None
    except (ValueError, FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def container_is_running(data_dir):
    try:
        result = _docker(
            "inspect", "--format", "{{.State.Running}}", container_name(data_dir),
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def stop_container(data_dir, timeout=15):
    """Only report a stopped tenant after Docker confirms its removal."""
    name = container_name(data_dir)
    network = network_name(data_dir)
    try:
        _docker("stop", "--time", str(max(1, int(timeout))), name,
                timeout=max(10, int(timeout) + 10))
        removed = _docker("rm", "--force", name, timeout=20)
        if removed.returncode and "No such container" not in removed.stderr:
            return False
        inspected = _docker("inspect", "--type", "container", name, timeout=10)
        if inspected.returncode == 0 or not any(message in inspected.stderr for message in ("No such object", "No such container")):
            return False
        _docker("network", "rm", network, timeout=20)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return True
