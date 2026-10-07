"""Keep a new tenant inert until the root agent verifies its actual bind mount.

This bootstrap imports only the standard library. It does not read tenant
configuration or touch tenant data. The server's release socket belongs to the
container's network namespace, which the portal cannot enter. Task containers
remain inert while the agent runs their separately verified command with
``docker exec``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import signal
import socket
import sys
import time

_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_RELEASE = b"mount-verified\n"
_POLL_SECONDS = 0.05


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("The mount gate token must be 32 lowercase hexadecimal characters")
    return value


def _address(token: str) -> bytes:
    return b"\0bananawiki-mount-" + _token(token).encode("ascii")


def _seal_process() -> None:
    """Refuse same-UID host ptrace or proc-root access while the guard waits."""
    if sys.platform != "linux":
        raise ValueError("The tenant mount gate requires Linux process isolation")
    native = ctypes.CDLL(None, use_errno=True)
    native.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    native.prctl.restype = ctypes.c_int
    if native.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "The tenant mount guard could not disable process tracing")
    if native.prctl(3, 0, 0, 0, 0) != 0:  # PR_GET_DUMPABLE
        raise ValueError("The tenant mount guard process remains traceable")


def release(token: str, *, lifetime: float = 10) -> None:
    """The agent executes this inside the already verified container/netns."""
    address = _address(token)
    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(min(1, max(0.001, deadline - time.monotonic())))
            try:
                client.connect(address)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
            else:
                client.sendall(_RELEASE)
                return
        time.sleep(_POLL_SECONDS)
    raise TimeoutError("The verified tenant mount gate did not become available")


def wait(token: str, *, lifetime: float = 120) -> bool:
    deadline = time.monotonic() + lifetime
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(_address(token))
        listener.listen(1)
        listener.settimeout(max(0.001, deadline - time.monotonic()))
        try:
            connection, _peer = listener.accept()
            with connection:
                payload = b""
                while len(payload) <= len(_RELEASE):
                    connection.settimeout(min(1, max(0.001, deadline - time.monotonic())))
                    chunk = connection.recv(len(_RELEASE) + 1 - len(payload))
                    if not chunk:
                        break
                    payload += chunk
                if payload != _RELEASE:
                    raise ValueError("The tenant mount gate release message is invalid")
                return True
        except TimeoutError:
            return False


def _lifetime(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 3:
        raise ValueError("The inert task lifetime must be an integer between 5 and 630 seconds")
    seconds = int(value)
    if str(seconds) != value or not 5 <= seconds <= 630:
        raise ValueError("The inert task lifetime must be an integer between 5 and 630 seconds")
    return seconds


def _idle(lifetime: int) -> None:
    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))


def _terminate(number: int, _frame: object) -> None:
    raise SystemExit(128 + number)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if (len(arguments) < 2 or arguments[0] not in {"server", "task", "release"}
            or len(arguments) > (3 if arguments[0] == "task" else 2)):
        print("Usage: tenant_guard server|release TOKEN; tenant_guard task TOKEN [SECONDS]", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        token = _token(arguments[1])
        if arguments[0] == "release":
            release(token)
            return 0
        _seal_process()
        if arguments[0] == "task":
            _idle(_lifetime(arguments[2]) if len(arguments) == 3 else 140)
            return 124
        if not wait(token):
            print("The root agent did not release this tenant's verified mount", file=sys.stderr)
            return 124
        # The executable and module are fixed by the trusted image, never IPC.
        os.execv(sys.executable, [sys.executable, "-E", "-s", "-m", "bananawiki.ops.tenant_entrypoint"])  # noqa: S606
        return 0
    except (OSError, ValueError) as error:
        print(f"Tenant mount gate refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
