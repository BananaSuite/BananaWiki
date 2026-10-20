"""A Docker daemon and systemd, simulated closely enough to run the real runtime agent end to end.

:class:`DockerHost` answers exactly the ``docker`` and ``systemctl`` command
lines :class:`bananawiki.ops.runtime_agent.TenantRuntime` sends: containers
(started behind the mount gate, with their labels, security options, sysctls
and bridge address), tenant networks (with or without the ``bwt…`` bridge
name 1.7 gives them) and Caddy, which may be missing altogether (a host whose
reverse proxy is another one). Nothing touches the real host.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from bananawiki.ops.runtime_agent import MOUNT_INVENTORY_FORMAT

BRIDGE_OPTION = "com.docker.network.bridge.name"


def _done(command: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class DockerHost:
    def __init__(self, *, caddy: bool = False):
        self.caddy = caddy  # an active caddy.service that reloads
        self.containers: dict[str, dict[str, Any]] = {}  # by name
        self.networks: dict[str, dict[str, Any]] = {}  # by name: internal, bridge ("" for a pre-1.7 br-<id>)
        self.calls: list[list[str]] = []
        self.addresses = 1
        self.released: list[str] = []  # containers whose mount gate was released

    # Test helpers

    def legacy_network(self, name: str, *, internal: bool = True) -> None:
        """A tenant network created by the 1.6 agent: Docker named its bridge br-<id>."""
        self.networks[name] = {"internal": internal, "bridge": ""}

    def runs(self) -> list[list[str]]:
        return [command for command in self.calls if command[:2] == ["docker", "run"]]

    def running(self) -> list[str]:
        return sorted(name for name, item in self.containers.items() if item["running"])

    # The command line

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess:
        command = [str(part) for part in command]
        self.calls.append(command)
        if command[0] == "systemctl":
            return self._systemctl(command)
        if command[0] == "caddy":
            raise FileNotFoundError("caddy")
        if command[0] != "docker":
            return _done(command, 127, "", f"{command[0]}: not found")
        verb = command[1]
        handler = getattr(self, "_" + verb.replace("-", "_"), None)
        if handler is None:
            return _done(command)
        return handler(command)

    def _systemctl(self, command: list[str]) -> subprocess.CompletedProcess:
        unit = command[-1]
        if not unit.startswith("caddy"):
            return _done(command)
        if not self.caddy:
            if command[1] == "is-active":
                return _done(command, 4, "", "")
            return _done(command, 5, "", "Failed to reload caddy.service: Unit caddy.service not found.")
        return _done(command)

    def _version(self, command: list[str]) -> subprocess.CompletedProcess:
        return _done(command, 0, "28.0.0\n")

    def _ps(self, command: list[str]) -> subprocess.CompletedProcess:
        filters = [command[index + 1] for index, part in enumerate(command) if part == "--filter"]
        everything = "--all" in command
        found = []
        for name, item in self.containers.items():
            if not everything and not item["running"]:
                continue
            if any(not self._matches(name, item, value) for value in filters):
                continue
            found.append(item["id"])
        return _done(command, 0, "".join(identifier + "\n" for identifier in found))

    @staticmethod
    def _matches(name: str, item: dict[str, Any], value: str) -> bool:
        key, _, wanted = value.partition("=")
        if key == "label":
            label, _, label_value = wanted.partition("=")
            if label not in item["labels"]:
                return False
            return not label_value or item["labels"][label] == label_value
        if key == "name":
            return re.search(wanted, "/" + name) is not None
        return True

    def _find(self, reference: str) -> tuple[str, dict[str, Any]] | None:
        for name, item in self.containers.items():
            if reference in (name, item["id"]) or (len(reference) >= 12 and item["id"].startswith(reference)):
                return name, item
        return None

    def _inspect(self, command: list[str]) -> subprocess.CompletedProcess:
        arguments = command[2:]
        if arguments[:2] == ["--type", "container"]:
            arguments = arguments[2:]
        template = None
        if arguments[:1] == ["--format"]:
            template, arguments = arguments[1], arguments[2:]
        found = [self._find(reference) for reference in arguments]
        missing = [reference for reference, item in zip(arguments, found, strict=True) if item is None]
        present = [item for item in found if item is not None]
        if template == "{{.State.Pid}}":
            if missing:
                return _done(command, 1, "", f"Error: No such container: {missing[0]}")
            return _done(command, 0, f"{present[0][1]['pid'] if present[0][1]['running'] else 0}\n")
        if template == MOUNT_INVENTORY_FORMAT:  # the agent's search for live mounts of a tenant directory
            lines = [json.dumps({"id": item["id"], "name": "/" + name, "role": item["labels"].get("org.bananawiki.role"),
                                 "running": item["running"], "pid": item["pid"] if item["running"] else 0})
                     for name, item in present]
            return _done(command, 1 if missing else 0, "".join(line + "\n" for line in lines))
        if template is not None:
            return _done(command, 1, "", "unsupported template in the fake")
        documents = [self._document(name, item) for name, item in present]
        stderr = "".join(f"Error: No such container: {reference}\n" for reference in missing)
        return _done(command, 1 if missing else 0, json.dumps(documents), stderr)

    def _document(self, name: str, item: dict[str, Any]) -> dict[str, Any]:
        network = item["network"]
        return {
            "Id": item["id"], "Name": "/" + name,
            "Config": {"Labels": dict(item["labels"]), "Image": item["image"]},
            "State": {"Running": item["running"], "Pid": item["pid"] if item["running"] else 0,
                      "StartedAt": item["started_at"]},
            "HostConfig": {"SecurityOpt": list(item["security"]), "Sysctls": dict(item["sysctls"])},
            "NetworkSettings": {"Networks": {network: {"IPAddress": item["address"] if item["running"] else ""}}},
        }

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        arguments = command[2:]
        options: dict[str, list[str]] = {}
        index = 0
        flags_with_values = {"--name", "--network", "--cap-drop", "--security-opt", "--sysctl", "--tmpfs", "--user", "--mount",
                             "--env-file", "--pids-limit", "--memory", "--memory-swap", "--cpus", "--ulimit",
                             "--log-driver", "--log-opt", "--restart", "--label", "--publish", "--entrypoint"}
        while index < len(arguments) and arguments[index].startswith("--"):
            flag = arguments[index]
            if flag in flags_with_values:
                options.setdefault(flag, []).append(arguments[index + 1])
                index += 2
            else:
                options.setdefault(flag, []).append("")
                index += 1
        image = arguments[index]
        name = options["--name"][0]
        if name in self.containers:
            return _done(command, 125, "", f"Conflict. The container name \"/{name}\" is already in use.")
        network = options["--network"][0]
        if network != "none" and network not in self.networks:
            return _done(command, 125, "", f"network {network} not found")
        security = []
        for value in options.get("--security-opt", []):
            if value.startswith("seccomp="):
                value = "seccomp=" + Path(value.removeprefix("seccomp=")).read_text()
            security.append(value)
        self.addresses += 1
        self.containers[name] = {
            "id": hashlib.sha256(f"{name}-{self.addresses}".encode()).hexdigest(), "image": image,
            "labels": dict(label.split("=", 1) for label in options.get("--label", [])),
            "running": True, "pid": 1000 + self.addresses, "network": network,
            "address": f"172.30.{self.addresses // 250}.{self.addresses % 250 + 2}" if network != "none" else "",
            "started_at": f"2026-10-10T10:00:{self.addresses % 60:02d}.000000000Z", "security": security,
            "sysctls": dict(value.split("=", 1) for value in options.get("--sysctl", [])),
            "env": Path(options["--env-file"][0]).read_text() if "--env-file" in options else "",
            "mount": next((value for value in options.get("--mount", [])), ""),
            "gated": "--entrypoint" in options,
        }
        return _done(command, 0, self.containers[name]["id"] + "\n")

    def _exec(self, command: list[str]) -> subprocess.CompletedProcess:
        name = command[3] if command[2] == "-i" else command[2]
        found = self._find(name)
        if found is None or not found[1]["running"]:
            return _done(command, 1, "", f"Error response from daemon: container {name} is not running")
        if "tenant_guard" in " ".join(command) and "release" in command:
            found[1]["gated"] = False
            self.released.append(found[0])
            return _done(command)
        return _done(command, 0, '{"ok": true, "users": []}\n')

    def _stop(self, command: list[str]) -> subprocess.CompletedProcess:
        found = self._find(command[-1])
        if found is None:
            return _done(command, 1, "", f"Error response from daemon: No such container: {command[-1]}")
        found[1]["running"] = False
        return _done(command)

    def _rm(self, command: list[str]) -> subprocess.CompletedProcess:
        missing = []
        for reference in command[3:]:
            found = self._find(reference)
            if found is None:
                missing.append(reference)
            else:
                del self.containers[found[0]]
        if missing:
            return _done(command, 1, "", "".join(f"Error response from daemon: No such container: {reference}\n"
                                                 for reference in missing))
        return _done(command)

    def _logs(self, command: list[str]) -> subprocess.CompletedProcess:
        return _done(command, 0, "")

    def _network(self, command: list[str]) -> subprocess.CompletedProcess:
        action, arguments = command[2], command[3:]
        if action == "inspect":
            template = None
            if arguments[:1] == ["--format"]:
                template, arguments = arguments[1], arguments[2:]
            item = self.networks.get(arguments[0])
            if item is None:
                return _done(command, 1, "", f"Error response from daemon: network {arguments[0]} not found")
            if template is None:
                return _done(command, 0, "[{}]")
            if template == "{{.EnableIPv6}}":
                return _done(command, 0, "false\n")
            return _done(command, 0, f"{str(item['internal']).lower()} {item['bridge'] or '<no value>'}\n")
        if action == "rm":
            name = arguments[0]
            if name not in self.networks:
                return _done(command, 1, "", f"Error response from daemon: No such network: {name}")
            if any(item["network"] == name for item in self.containers.values()):
                return _done(command, 1, "", "error while removing network: network has active endpoints")
            del self.networks[name]
            return _done(command)
        if action == "create":
            name = arguments[-1]
            bridge = next((value.split("=", 1)[1] for value in arguments if value.startswith(BRIDGE_OPTION + "=")), "")
            self.networks[name] = {"internal": "--internal" in arguments, "bridge": bridge}
            return _done(command, 0, name + "\n")
        return _done(command)
