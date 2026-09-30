"""Client the hosting portal uses to reach the runtime agent (see ``RUNTIME_AGENT.md``).

::

    from bananawiki.ops.agent_client import connect, AgentError

    runtime = connect()                      # from BW_RUNTIME_AGENT_SOCKET
    runtime.call("tenant.start", {"tenant": "acme", "env": {...}, "limits": {...}})

:func:`connect` returns an :class:`AgentClient` when the socket is configured.
Without it (a release installed by the 1.4 updater, whose portal still has
Docker access until the controller converges its units) it returns an
in-process :class:`~.runtime_agent.TenantRuntime` built from the portal's
own environment; both expose the same ``call(op, args)``.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .runtime_agent import PROTOCOL, AgentError, TenantRuntime, new_request_id

__all__ = ["AgentClient", "AgentError", "connect"]


class AgentClient:
    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 150.0):
        self.path = str(path)
        self.timeout = timeout

    def call(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        request = {"v": PROTOCOL, "id": new_request_id(), "op": op, "args": args or {}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(json.dumps(request).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    raw = stream.readline(4 * 1024 * 1024)
        except OSError as error:
            raise AgentError("unavailable", f"The runtime agent is not reachable: {type(error).__name__}") from None
        try:
            response = json.loads(raw)
        except json.JSONDecodeError:
            raise AgentError("protocol", "The runtime agent sent an invalid response.") from None
        if not response.get("ok"):
            error = response.get("error") or {}
            raise AgentError(str(error.get("code", "unknown")), str(error.get("message", "Request failed.")))
        return response["result"]


def local_runtime(environ: Mapping[str, str]) -> TenantRuntime:
    """The in-process fallback: same validation, run with the portal's own Docker access."""
    entry = pwd.getpwuid(os.geteuid())
    return TenantRuntime({
        "service": entry.pw_name, "service_uid": entry.pw_uid, "service_gid": entry.pw_gid,
        "instances_dir": environ.get("INSTANCES_DIR", ""),
        "image": environ.get("HOSTING_CONTAINER_IMAGE", "bananawiki-tenant:latest"),
        "limits": {"memory_mb": 4096, "cpus": 4.0, "pids": 4096, "nofile": 65536},
    })


def connect(environ: Mapping[str, str] | None = None) -> AgentClient | TenantRuntime:
    environ = os.environ if environ is None else environ
    path = environ.get("BW_RUNTIME_AGENT_SOCKET", "")
    if path:
        return AgentClient(Path(path))
    return local_runtime(environ)
