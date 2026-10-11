"""The host firewall for tenant containers (``deploy/nftables/bananawiki-tenants.nft``), filled in for this host.

Wikis with outbound networking may not reach private addresses, with one
exception: DNS to the resolvers this host uses. Docker's embedded resolver
forwards a wiki's queries from the wiki's own network to the host's
resolvers, so a private one (a VPC or LAN resolver) must be reachable on
port 53, and no other private address may be (internal DNS servers, zone
transfers, or any internal service that listens on port 53). ``render``
lists the resolvers in ``dns_resolvers_v4``; a resolver listening on one of
this host's own addresses is reached through the input chain, so port 53
joins the host ports instead.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

RULES = Path(__file__).resolve().parents[2] / "deploy/nftables/bananawiki-tenants.nft"
# Docker reads /etc/resolv.conf, or systemd-resolved's list of upstream resolvers when that file only names
# its local stub.
RESOLV_FILES = (Path("/etc/resolv.conf"), Path("/run/systemd/resolve/resolv.conf"))


def resolvers(files: Sequence[Path] = RESOLV_FILES) -> list[ipaddress.IPv4Address]:
    """The IPv4 resolvers Docker forwards tenant queries to: the first file naming any that is not loopback."""
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                try:
                    address = ipaddress.ip_address(fields[1].split("%", 1)[0])
                except ValueError:
                    continue
                if isinstance(address, ipaddress.IPv4Address) and not address.is_loopback and address not in found:
                    found.append(address)
        if found:
            return found
    return []


def is_local(address: ipaddress.IPv4Address) -> bool:
    """Whether *address* is one of this host's own (it can be bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((str(address), 0))
        except OSError:
            return False
    return True


def _set_elements(text: str, name: str, values: Iterable[str]) -> str:
    """*text* with *values* added to the elements of nftables set *name* (created when it has none)."""
    match = re.search(r"(\tset " + name + r" \{\n)(.*?)(\n\t\})", text, re.S)
    if match is None:
        raise ValueError(f"The firewall rules have no set {name}.")
    body = match.group(2)
    existing = re.search(r"elements = \{(.*?)\}", body, re.S)
    current = [item.strip() for item in existing.group(1).split(",") if item.strip()] if existing else []
    merged = current + [value for value in values if value not in current]
    if not merged:
        return text
    line = "\t\telements = { " + ", ".join(merged) + " }"
    body = (body[:existing.start()] + line.strip() + body[existing.end():]) if existing else body + "\n" + line
    return text[:match.start(2)] + body + text[match.end(2):]


def render(text: str, servers: Sequence[ipaddress.IPv4Address], *,
           local: Callable[[ipaddress.IPv4Address], bool] = is_local) -> str:
    """The rules *text* allowing DNS to *servers* where they need it."""
    remote = [str(server) for server in servers if not server.is_global and not local(server)]
    if remote:
        text = _set_elements(text, "dns_resolvers_v4", remote)
    if any(local(server) for server in servers):
        text = _set_elements(text, "host_udp_ports", ["53"])
        text = _set_elements(text, "host_tcp_ports", ["53"])
    return text


def rules() -> str:
    """The shipped rules filled in for this host."""
    return render(RULES.read_text(encoding="utf-8"), resolvers())
