"""Addresses and ports for the local wiki.

The wiki listens on the loopback interface unless the user explicitly chose
to share it on the local network. Nothing here contacts the internet: the
LAN address is found from the routing table (a UDP "connect" sends no
packet) and, for offline classrooms without a default route, from the
operating system's own interface listing.
"""

from __future__ import annotations

import errno
import ipaddress
import platform
import re
import socket
import subprocess
from collections.abc import Iterable

LOOPBACK_HOST = "127.0.0.1"
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - used only after the user consents to LAN sharing
DEFAULT_PORT = 80
FALLBACK_PORTS = (8080, *range(8000, 8100))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SKIP_LINE = ("gateway", "dhcp server", "dns server", "subnet mask")
_ADDRESS_LINE = (" inet ", "ipv4 address", "ip address")


def bind_host(share_on_lan: bool) -> str:
    """The listen address: loopback, or every interface when the user opted in."""
    return ALL_INTERFACES if share_on_lan else LOOPBACK_HOST


def is_usable_lan_ip(value: str) -> bool:
    """A private or link-local IPv4 address other devices on the LAN can reach."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version != 4 or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        return False
    return bool(ip.is_private or ip.is_link_local)


def addresses_in_text(text: str) -> list[str]:
    """Interface addresses in ``ip addr`` / ``ifconfig`` / ``ipconfig`` output."""
    found: list[str] = []
    for raw in text.splitlines():
        line = f" {raw.strip().lower()} "
        if any(marker in line for marker in _SKIP_LINE) or not any(m in line for m in _ADDRESS_LINE):
            continue
        match = _IPV4.search(raw)
        if match and is_usable_lan_ip(match.group()) and match.group() not in found:
            found.append(match.group())
    return found


def _run(args: Iterable[str]) -> str:
    try:
        completed = subprocess.run(list(args), capture_output=True, text=True, timeout=5, check=False,
                                   creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""


def _routed_address() -> str | None:
    for target in ("192.168.255.255", "10.255.255.255", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 9))
                address = sock.getsockname()[0]
        except OSError:
            continue
        if is_usable_lan_ip(address):
            return address
    return None


def _listed_addresses() -> list[str]:
    system = platform.system()
    if system == "Windows":
        commands = [["ipconfig"]]
    elif system == "Darwin":
        commands = [["ifconfig"]]
    else:
        commands = [["ip", "-4", "addr", "show"], ["ifconfig"]]
    for command in commands:
        found = addresses_in_text(_run(command))
        if found:
            return found
    return []


def lan_address() -> str | None:
    """This computer's address on the local network, or None when offline."""
    routed = _routed_address()
    if routed:
        return routed
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if is_usable_lan_ip(info[4][0]):
                return info[4][0]
    except OSError:
        pass
    candidates = _listed_addresses()
    candidates.sort(key=lambda ip: 0 if ipaddress.ip_address(ip).is_private else 1)
    return candidates[0] if candidates else None


def port_candidates(preferred: int | None) -> list[int]:
    """Ports to try in order: the user's choice (or 80), then common alternatives."""
    first = preferred or DEFAULT_PORT
    return [first, *(port for port in FALLBACK_PORTS if port != first)]


def is_address_in_use(error: OSError) -> bool:
    """Bind failures that mean "try the next port" (in use or privileged)."""
    return error.errno in {errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1),
                           getattr(errno, "WSAEACCES", -1), 10013, 10048}


def bound_socket(host: str, port: int) -> socket.socket:
    """A TCP socket bound to *host*:*port*, ready for the server to listen on.

    On Windows the port is taken for exclusive use: with ``SO_REUSEADDR``
    (what waitress sets) another program could bind the same port and
    receive the wiki's requests. POSIX keeps ``SO_REUSEADDR``, which there
    only lets a port in TIME_WAIT be reused.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


def port_available(host: str, port: int) -> bool:
    """Whether :func:`bound_socket` could take *host*:*port* now."""
    try:
        bound_socket(host, port).close()
    except OSError:
        return False
    return True


def url(host: str, port: int, path: str = "/") -> str:
    """An http URL; the port is omitted when it is 80."""
    netloc = host if port == 80 else f"{host}:{port}"
    return f"http://{netloc}{path}"
