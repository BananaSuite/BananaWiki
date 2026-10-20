"""A new wiki's first certificate: the handshake deadline and the ACME challenges of the generated Caddyfile.

Caddy orders the certificate of a wiki host inside the first visitor's TLS handshake. The adapted
configuration of the real Caddy (set CADDY_BINARY, or have caddy on PATH) is checked where it matters.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from bananawiki.ops import caddy, caddy_blocks
from bananawiki.ops.caddy import ProxyOptions
from bananawiki.ops.runtime_agent import render_routes

HOSTING = {"schema": 1, "product": "BananaWiki", "mode": "hosting", "root": "/opt/bananawiki",
           "service": "bananawiki", "domain": "example.org", "portal_domain": "portal.example.org", "port": 5099}
WIKI = {**HOSTING, "mode": "wiki", "domain": "wiki.example.org", "portal_domain": "", "port": 5001}
ALPN_OFF = "\tcert_issuer acme {\n\t\tdisable_tlsalpn_challenge\n\t}\n"
SECOND = 1_000_000_000


def adapt(tmp_path, settings, options=None) -> dict:
    binary = os.environ.get("CADDY_BINARY") or shutil.which("caddy")
    if not binary:
        pytest.skip("needs a caddy binary (CADDY_BINARY)")
    (tmp_path / "routes").mkdir(exist_ok=True)
    (tmp_path / "routes/tenants.caddy").write_text(render_routes(
        {"acme": (["acme-hosting.example.org", "docs.acme.net"], "172.18.0.2:5001")}, portal="127.0.0.1:5099"))
    config = tmp_path / "Caddyfile"
    config.write_text(caddy.render(settings, email="ops@example.org", routes=str(tmp_path / "routes"),
                                   options=options or ProxyOptions()))
    result = subprocess.run([binary, "adapt", "--config", str(config), "--adapter", "caddyfile"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    return json.loads(result.stdout)


def test_the_handshake_may_wait_for_the_certificate():
    assert caddy_blocks.READ_HEADER_TIMEOUT == "60s"
    for text in (caddy.render(HOSTING), caddy.render(WIKI), *map(caddy.example, ("wiki", "hosting", "compose"))):
        assert "\t\t\tread_header 60s\n\t\t\tread_body 5m\n\t\t\tidle 16m\n" in text


def test_adapted_timeouts_keep_the_handshake_deadline_above_an_acme_order(tmp_path):
    for options in (ProxyOptions(), ProxyOptions(cloudflare=True)):
        servers = adapt(tmp_path, HOSTING, options)["apps"]["http"]["servers"]
        assert servers
        for server in servers.values():
            assert server["read_header_timeout"] == 60 * SECOND
            assert server["read_timeout"] == 300 * SECOND, "the other limits are unchanged"
            assert server["idle_timeout"] == 16 * 60 * SECOND


def test_a_hosting_platform_never_uses_tls_alpn():
    assert ALPN_OFF in caddy.render(HOSTING)
    assert ALPN_OFF in caddy.render(HOSTING, options=ProxyOptions(cloudflare=True))
    for options in (ProxyOptions(tls="cloudflare-dns"),
                    ProxyOptions(tls="origin-cert", origin_cert="/etc/caddy/o.crt", origin_key="/etc/caddy/o.key")):
        assert ALPN_OFF in caddy.render(HOSTING, options=options)
    assert ALPN_OFF in caddy.example("hosting")


def test_a_single_wiki_keeps_caddys_default_issuers_unless_behind_cloudflare():
    assert "cert_issuer" not in caddy.render(WIKI)
    assert "cert_issuer" not in caddy.example("wiki") and "cert_issuer" not in caddy.example("compose")
    assert ALPN_OFF in caddy.render(WIKI, options=ProxyOptions(cloudflare=True))


@pytest.mark.parametrize("options", [ProxyOptions(), ProxyOptions(cloudflare=True)], ids=["acme", "cloudflare"])
def test_adapted_hosting_policies_all_disable_tls_alpn(tmp_path, options):
    tls = adapt(tmp_path, HOSTING, options)["apps"]["tls"]["automation"]
    policies = tls["policies"]
    assert any(policy.get("on_demand") for policy in policies), "wiki hosts are issued on demand"
    assert any("example.org" in policy.get("subjects", []) for policy in policies), "and the platform's own hosts"
    for policy in policies:
        (issuer,) = policy["issuers"]
        assert issuer["module"] == "acme"
        assert issuer["challenges"]["tls-alpn"] == {"disabled": True}
        assert "http" not in issuer["challenges"], "HTTP-01 stays on"
    assert tls["on_demand"]["permission"]["endpoint"] == "http://127.0.0.1:5099/internal/domains/authorize"


def test_adapted_single_wiki_without_cloudflare_has_no_issuer_override(tmp_path):
    tls = adapt(tmp_path, WIKI)["apps"]["tls"]["automation"]
    assert all("issuers" not in policy or "challenges" not in policy["issuers"][0] for policy in tls["policies"])
