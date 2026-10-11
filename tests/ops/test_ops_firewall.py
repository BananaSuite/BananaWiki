"""The default host firewall for tenant containers (deploy/nftables/bananawiki-tenants.nft).

The rules match tenant bridges by the interface names the runtime agent gives
them; these tests keep the two in step. ``nft --check`` runs where nft may
talk to the kernel (root, as in CI's root boundary step); elsewhere it is
skipped and only the file's structure is checked.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bananawiki.ops.runtime_agent import BRIDGE_PREFIX, bridge_name

RULES = Path(__file__).resolve().parents[2] / "deploy/nftables/bananawiki-tenants.nft"


def text() -> str:
    return RULES.read_text(encoding="utf-8")


def set_elements(name: str) -> list[str]:
    match = re.search(r"set " + name + r" \{(.*?)\n\t\}", text(), re.S)
    assert match, name
    elements = re.search(r"elements = \{(.*?)\}", match.group(1), re.S)
    return [item.strip() for item in elements.group(1).split(",")] if elements else []


def test_the_rules_match_exactly_the_bridges_the_agent_names():
    assert re.fullmatch(BRIDGE_PREFIX + r"[0-9a-f]{12}", bridge_name("bananawiki-acme-0123456789ab-net"))
    matched = set(re.findall(r'(?:iif|oif)name "([^"]+)"', text()))
    assert matched == {BRIDGE_PREFIX + "*"}


def test_tenants_cannot_reach_the_host_or_private_networks_by_default():
    rules = text()
    assert "type filter hook input" in rules and "type filter hook forward" in rules
    # Only Caddy on the host by default; nothing is allowed on the private side.
    assert set_elements("host_tcp_ports") == ["80", "443"]
    assert set_elements("host_udp_ports") == [] and set_elements("allowed_private_v4") == []
    blocked = [ipaddress.ip_network(item) for item in set_elements("non_public_v4")]
    for address in ("169.254.169.254", "10.1.2.3", "172.17.0.1", "192.168.1.1", "100.64.0.1", "127.0.0.1",
                    "224.0.0.251", "255.255.255.255", "0.0.0.0"):
        assert any(ipaddress.ip_address(address) in network for network in blocked), address
    for address in ("1.1.1.1", "8.8.8.8", "140.82.112.3"):
        assert not any(ipaddress.ip_address(address) in network for network in blocked), address
    # Each tenant chain ends by dropping what it did not allow.
    assert re.search(r"chain tenant_to_host \{.*\n\t\tcounter drop\n\t\}", rules, re.S)
    assert re.search(r"chain tenant_forward \{.*\n\t\tip daddr @non_public_v4 counter drop\n\t\}", rules, re.S)


def test_loading_the_file_again_replaces_only_its_own_table():
    rules = text()
    assert "flush ruleset" not in rules
    assert rules.index("table inet bananawiki_tenants\ndelete table inet bananawiki_tenants\n") < \
        rules.index("table inet bananawiki_tenants {")
    assert set(re.findall(r"^table (\w+ \w+)", rules, re.M)) == {"inet bananawiki_tenants"}


def test_nft_accepts_the_rules():
    nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
    if nft is None:
        pytest.skip("nft is not installed")
    result = subprocess.run([nft, "--check", "--file", str(RULES)], capture_output=True, text=True, timeout=30,
                            check=False)
    if result.returncode and "Operation not permitted" in result.stderr:
        pytest.skip("nft needs CAP_NET_ADMIN to check a ruleset (run as root)")
    assert result.returncode == 0, result.stderr


# DNS: only to the resolvers this host uses (OPS2-07) -----------------------------------------------------


def test_dns_is_allowed_only_to_the_resolvers_listed_not_to_every_private_address():
    rules = text()
    assert "dport 53" in rules
    for line in rules.splitlines():
        if "dport 53" in line:
            assert "@dns_resolvers_v4" in line and "@non_public_v4" not in line, line
    assert set_elements("dns_resolvers_v4") == []


def elements(rules: str, name: str) -> list[str]:
    match = re.search(r"set " + name + r" \{(.*?)\n\t\}", rules, re.S)
    assert match, name
    found = re.search(r"elements = \{(.*?)\}", match.group(1), re.S)
    return [item.strip() for item in found.group(1).split(",")] if found else []


def test_the_rendered_rules_list_this_hosts_resolvers(tmp_path):
    from bananawiki.ops import firewall

    stub = tmp_path / "resolv.conf"
    stub.write_text("# systemd-resolved\nnameserver 127.0.0.53\noptions edns0\n")
    upstream = tmp_path / "upstream-resolv.conf"
    upstream.write_text("nameserver 10.0.0.2\nnameserver 1.1.1.1\nnameserver 2001:db8::1\nnameserver bogus\n")
    servers = firewall.resolvers((stub, upstream))
    assert [str(server) for server in servers] == ["10.0.0.2", "1.1.1.1"]
    rendered = firewall.render(text(), servers, local=lambda _address: False)
    assert elements(rendered, "dns_resolvers_v4") == ["10.0.0.2"]  # a public resolver needs no exception
    assert elements(rendered, "host_udp_ports") == [] and elements(rendered, "host_tcp_ports") == ["80", "443"]
    # A resolver listening on one of this host's own addresses is reached through the input chain.
    local = firewall.render(text(), [ipaddress.ip_address("192.168.1.10")],
                            local=lambda address: str(address) == "192.168.1.10")
    assert elements(local, "dns_resolvers_v4") == []
    assert elements(local, "host_udp_ports") == ["53"] and elements(local, "host_tcp_ports") == ["80", "443", "53"]
    assert firewall.render(text(), [], local=lambda _address: False) == text()
    assert firewall.RULES == RULES
