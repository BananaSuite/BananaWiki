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

Nothing loads the rules by itself, and they are gone after a reboot unless
the host's nftables service loads a saved copy. ``status``, ``start``,
``restart``, ``update`` and ``install`` therefore check that the table is
loaded (:meth:`.System.tenant_firewall`), warn when it is not, and keep the
answer in ``config/host-firewall.json`` with the boot it was made in; the
runtime agent passes that record on to the portal (:func:`recorded`), whose
administrators' dashboard warns while the table is missing or was not
checked since the host started.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RULES = Path(__file__).resolve().parents[2] / "deploy/nftables/bananawiki-tenants.nft"
# Docker reads /etc/resolv.conf, or systemd-resolved's list of upstream resolvers when that file only names
# its local stub.
RESOLV_FILES = (Path("/etc/resolv.conf"), Path("/run/systemd/resolve/resolv.conf"))
# The table the rules keep to (``nft list table inet bananawiki_tenants``).
TABLE = ("inet", "bananawiki_tenants")
STAMP_PREFIX = "bananawiki rules "
# The latest check, in the installation's config directory (root's).
RECORD_NAME = "host-firewall.json"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


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


def stamp(text: str) -> str:
    """*text* with a comment on the table naming the digest of the rules, so a loaded copy that is empty,
    older or made for other resolvers can be told from the current one (:func:`loaded_digest`)."""
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    head = "table inet " + TABLE[1] + " {\n"
    if head not in text:
        raise ValueError("The firewall rules have no table " + " ".join(TABLE) + ".")
    return text.replace(head, head + f'\tcomment "{STAMP_PREFIX}{digest}"\n', 1)


def loaded_digest(listing: str) -> str | None:
    """The digest stamped on a loaded table (``nft list table`` output), or None without one."""
    match = re.search(r'comment "' + re.escape(STAMP_PREFIX) + r'([0-9a-f]{16})"', listing)
    return match.group(1) if match else None


def expected_digest() -> str:
    return loaded_digest(rules()) or ""


def rules() -> str:
    """The shipped rules filled in for this host, stamped with their digest."""
    return stamp(render(RULES.read_text(encoding="utf-8"), resolvers()))


# Whether the rules are loaded


def boot_id(path: Path = BOOT_ID) -> str:
    """This boot of the host (the kernel's random boot id), or "" when unknown: a reboot drops the rules."""
    try:
        return path.read_text(encoding="ascii").strip()[:64]
    except (OSError, UnicodeDecodeError):
        return ""


def warning(problem: str) -> str:
    """What the operator is told when the table is not loaded (*problem* says why)."""
    return (f"The host firewall for tenant containers is not loaded ({problem}): wikis can connect to every "
            "service on this host (SSH, databases, anything listening on 0.0.0.0) and, with outbound networking, "
            "to the LAN and cloud metadata endpoints. Load it with 'sudo bananawiki firewall | sudo nft -f -' and "
            "make it persistent (docs/hosting.md#host-firewall).")


def record(problem: str | None, *, boot: str | None = None) -> dict[str, Any]:
    """What ``config/host-firewall.json`` keeps of a check (*problem*: None when the table is loaded)."""
    return {"loaded": problem is None, "reason": (problem or "")[:500], "boot_id": boot_id() if boot is None else boot,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}


def recorded(path: Path, *, boot: str | None = None) -> dict[str, Any]:
    """The latest check as the portal shows it: ``loaded`` is None when it is unknown (never checked, or checked
    before the host last started, which drops rules that were not made persistent)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("loaded"), bool):
        return {"loaded": None, "reason": "", "checked_at": ""}
    current = boot_id() if boot is None else boot
    stale = not current or data.get("boot_id") != current
    return {"loaded": None if stale else data["loaded"], "reason": str(data.get("reason") or "")[:500],
            "checked_at": str(data.get("checked_at") or "")[:40]}
