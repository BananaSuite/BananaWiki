"""Serving behind Cloudflare's proxy: the rendered Caddyfiles, the certificate modes and `proxy`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ops_fakes import FakeSystem  # type: ignore[import-not-found]

from bananawiki.core import cloudflare
from bananawiki.ops import caddy, caddy_blocks
from bananawiki.ops import cli as ops_cli
from bananawiki.ops.caddy import ProxyOptions
from bananawiki.ops.files import read_environment, read_json, write_environment, write_json
from bananawiki.ops.manager import Manager
from bananawiki.ops.runtime_agent import render_routes

HOSTING = {"schema": 1, "product": "BananaWiki", "mode": "hosting", "root": "/opt/bananawiki",
           "service": "bananawiki", "domain": "example.org", "portal_domain": "portal.example.org", "port": 5099}
WIKI = {**HOSTING, "mode": "wiki", "domain": "wiki.example.org", "portal_domain": "", "port": 5001}
TOKEN = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hJ7k"  # noqa: S105 - test fixture
RANGES = " ".join(cloudflare.RANGES)


def blocks(text: str) -> dict[str, str]:
    """Top-level site blocks by their address line."""
    output, current, depth, lines = {}, None, 0, []
    for line in text.splitlines():
        if depth == 0 and line.endswith(" {") and not line.startswith(("(", "{")):
            current, lines = line[:-2], []
        if current is not None:
            lines.append(line)
        depth += line.count("{") - line.count("}")
        if current is not None and depth == 0:
            output[current] = "\n".join(lines)
            current = None
    return output


# Cloudflare ranges ---------------------------------------------------------------


def test_cloudflare_addresses():
    assert cloudflare.is_cloudflare_address("104.16.1.1") and cloudflare.is_cloudflare_address("2606:4700::1")
    assert cloudflare.is_cloudflare_address("::ffff:172.64.0.1")
    assert not cloudflare.is_cloudflare_address("203.0.113.5") and not cloudflare.is_cloudflare_address("nonsense")
    assert cloudflare.all_cloudflare(["104.16.1.1", "172.67.2.2"])
    assert not cloudflare.all_cloudflare([]) and not cloudflare.all_cloudflare(["104.16.1.1", "198.51.100.1"])
    assert len(cloudflare.IPV4) == 15 and len(cloudflare.IPV6) == 7 and cloudflare.UPDATED


def test_caddy_versions():
    assert caddy_blocks.parse_version("v2.10.2 h1:g/gT=") == (2, 10, 2)
    assert caddy_blocks.parse_version("") is None
    assert caddy_blocks.supports_client_ip(None) and caddy_blocks.supports_client_ip((2, 7, 0))
    assert not caddy_blocks.supports_client_ip((2, 6, 2))


# Rendered Caddyfiles -------------------------------------------------------------


def test_every_site_answers_http_without_a_cloudflare_redirect_loop():
    text = caddy.render(HOSTING)
    sites = blocks(text)
    assert set(sites) == {"example.org, http://example.org", "www.example.org, http://www.example.org",
                          "portal.example.org, http://portal.example.org", ":443, :80"}
    for address in ("example.org, http://example.org", "portal.example.org, http://portal.example.org", ":443, :80"):
        assert "\timport bananawiki_https\n" in sites[address], address
    policy = text[text.index("(bananawiki_https) {"):]
    assert "\t@needs_https {\n\t\tprotocol http\n\t\tnot {\n\t\t\tremote_ip " + RANGES + "\n" in policy
    assert '\t\t\theader CF-Visitor *"scheme":"https"*\n' in policy
    assert "\tredir @needs_https https://{host}{uri} 308\n" in policy
    wiki = caddy.render(WIKI)
    assert "wiki.example.org, http://wiki.example.org {" in wiki and "redir @needs_https" in wiki


def test_real_client_addresses_come_from_cloudflare_only():
    for text in (caddy.render(HOSTING), caddy.render(WIKI)):
        assert f"\t\ttrusted_proxies static {RANGES}\n\t\tclient_ip_headers CF-Connecting-IP\n" in text
        assert "\t\t\tidle 16m\n" in text, "longer than Cloudflare's 900 s reuse of idle connections"
        assert text.count("reverse_proxy ") == text.count("header_up X-Forwarded-For {client_ip}")
    legacy = caddy.render(HOSTING, options=ProxyOptions(client_ip=False))
    assert "trusted_proxies" not in legacy and "{client_ip}" not in legacy


def test_downloads_have_no_total_time_limit():
    """Caddy's `write` timeout covers a whole response, and `write_idle` breaks Caddy before 2.11.6 (R-27):
    stalled downloads are dropped by the application server and by recent Caddy's default."""
    for text in (caddy.render(HOSTING), caddy.render(WIKI), *map(caddy.example, ("wiki", "hosting", "compose"))):
        timeouts = text[text.index("\t\ttimeouts {\n"):]
        assert "\t\t\twrite" not in timeouts[:timeouts.index("\t\t}\n")]


def test_www_redirects_to_the_base_domain():
    sites = blocks(caddy.render(HOSTING))
    www = sites["www.example.org, http://www.example.org"]
    assert "\ttls {\n\t\ton_demand\n\t}\n" in www and "redir https://example.org{uri} 308" in www
    portal_on_www = caddy.render({**HOSTING, "portal_domain": "www.example.org"})
    assert "redir https://example.org{uri}" not in portal_on_www


def test_acme_behind_cloudflare_skips_tls_alpn():
    assert "cert_issuer" not in caddy.render(HOSTING)
    for settings in (HOSTING, WIKI):
        text = caddy.render(settings, options=ProxyOptions(cloudflare=True))
        assert "\tcert_issuer acme {\n\t\tdisable_tlsalpn_challenge\n\t}\n" in text


def test_cloudflare_dns_mode_uses_one_wildcard_certificate():
    text = caddy.render(HOSTING, options=ProxyOptions(tls="cloudflare-dns"))
    sites = blocks(text)
    dns = "\ttls {\n\t\tdns cloudflare {env.CLOUDFLARE_API_TOKEN}\n\t}\n"
    assert dns in sites["example.org, http://example.org"]
    wildcard = sites["*.example.org, http://*.example.org"]
    assert dns in wildcard and "reverse_proxy 127.0.0.1:5099" in wildcard
    assert "tls" not in sites["www.example.org, http://www.example.org"], "covered by the wildcard"
    assert "tls" not in sites["portal.example.org, http://portal.example.org"]
    assert "on_demand" in sites[":443, :80"], "custom domains keep on-demand certificates"
    assert "disable_tlsalpn_challenge" in text and "prefer_wildcard" not in text
    assert text.index("*.example.org") < text.index("import /var/lib/bananawiki-routes")
    wiki = caddy.render(WIKI, options=ProxyOptions(tls="cloudflare-dns"))
    assert dns in wiki and "*." not in wiki


def test_origin_certificate_mode_splits_http_blocks():
    options = ProxyOptions(tls="origin-cert", origin_cert="/etc/caddy/o.crt", origin_key="/etc/caddy/o.key")
    sites = blocks(caddy.render(HOSTING, options=options))
    tls = "\ttls /etc/caddy/o.crt /etc/caddy/o.key\n"
    for host in ("example.org", "www.example.org", "portal.example.org", "*.example.org"):
        assert tls in sites[host], host
        assert "tls" not in sites["http://" + host], host
    other_portal = blocks(caddy.render({**HOSTING, "portal_domain": "hosting.example.net"}, options=options))
    assert "tls" not in other_portal["hosting.example.net, http://hosting.example.net"]
    wiki = blocks(caddy.render(WIKI, options=options))
    assert tls in wiki["wiki.example.org"] and "reverse_proxy" in wiki["http://wiki.example.org"]


@pytest.mark.parametrize("kwargs", [
    {"tls": "letsencrypt"}, {"tls": "origin-cert"}, {"tls": "origin-cert", "origin_cert": "/c", "origin_key": ""},
    {"tls": "origin-cert", "origin_cert": "relative.crt", "origin_key": "/k"},
    {"tls": "origin-cert", "origin_cert": "/c {\n}", "origin_key": "/k"},
])
def test_proxy_options_are_validated(kwargs):
    with pytest.raises(ValueError):
        ProxyOptions(**kwargs)


def test_proxy_options_round_trip_through_proxy_json():
    options = ProxyOptions(tls="origin-cert", cloudflare=True, origin_cert="/c", origin_key="/k")
    assert caddy.options_for({"tls": options.record()}, (2, 10, 0)) == options
    assert caddy.options_for({}, None) == ProxyOptions()
    assert caddy.options_for({"tls": {"mode": "cloudflare-dns"}}, (2, 6, 2)).client_ip is False
    assert ProxyOptions(tls="cloudflare-dns").behind_cloudflare and not ProxyOptions().behind_cloudflare


def test_universal_ssl_depth_warnings():
    assert caddy.layout_warnings(HOSTING) == []
    assert caddy.layout_warnings({**HOSTING, "domain": "example.co.uk", "portal_domain": "portal.example.co.uk"}) == []
    nested = caddy.layout_warnings({**HOSTING, "domain": "hosting.example.org",
                                    "portal_domain": "portal.hosting.example.org"})
    assert len(nested) == 2 and all("ERR_SSL_VERSION_OR_CIPHER_MISMATCH" in item for item in nested)
    assert "--domain example.org --portal-domain hosting.example.org" in nested[0]
    assert caddy.layout_warnings(WIKI) == []
    assert caddy.layout_warnings({**WIKI, "domain": "wiki.team.example.org"})
    assert caddy.layout_warnings({**HOSTING, "domain": ""}) == []


# Real Caddy (set CADDY_BINARY, or have caddy on PATH) ------------------------------


def _caddy() -> str | None:
    return os.environ.get("CADDY_BINARY") or shutil.which("caddy")


@pytest.mark.parametrize("mode", ["acme", "cloudflare", "origin-cert", "legacy", "cloudflare-dns"])
@pytest.mark.parametrize("settings", [HOSTING, WIKI], ids=["hosting", "wiki"])
def test_rendered_caddyfiles_validate(tmp_path, mode, settings, monkeypatch):
    binary = os.environ.get("CADDY_CLOUDFLARE_BINARY") if mode == "cloudflare-dns" else _caddy()
    if not binary:
        pytest.skip("needs a caddy binary (CADDY_BINARY; CADDY_CLOUDFLARE_BINARY with caddy-dns/cloudflare)")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    certificate, key = tmp_path / "origin.crt", tmp_path / "origin.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(certificate), "-days", "2", "-subj", "/CN=example.org"], check=True, capture_output=True)
    options = {"acme": ProxyOptions(), "cloudflare": ProxyOptions(cloudflare=True),
               "origin-cert": ProxyOptions(tls="origin-cert", origin_cert=str(certificate), origin_key=str(key)),
               "legacy": ProxyOptions(client_ip=False), "cloudflare-dns": ProxyOptions(tls="cloudflare-dns")}[mode]
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes/tenants.caddy").write_text(render_routes(
        {"acme": (["acme-hosting.example.org", "docs.acme.net"], "172.18.0.2:5001")}, portal="127.0.0.1:5099"))
    config = tmp_path / "Caddyfile"
    config.write_text(caddy.render(settings, email="ops@example.org", routes=str(tmp_path / "routes"),
                                   options=options))
    result = subprocess.run([binary, "validate", "--config", str(config), "--adapter", "caddyfile"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    formatted = subprocess.run([binary, "fmt", str(config)], capture_output=True, text=True)
    assert formatted.stdout == config.read_text(), "the rendered file is already `caddy fmt` clean"


# `bananawiki proxy` ----------------------------------------------------------------


class CaddyHost(FakeSystem):
    """A fake host whose Caddy reports a version and its modules."""

    def __init__(self, tmp: Path, version: str = "v2.10.2 h1:x", modules: str = "http.handlers.file_server\n"):
        super().__init__(tmp)
        self.version, self.modules = version, modules
        self.validated_env: list[dict[str, str] | None] = []

    def _run(self, command, **kwargs):
        if command[:2] == ["caddy", "version"]:
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, self.version, "")
        if command[:2] == ["caddy", "list-modules"]:
            return subprocess.CompletedProcess(command, 0, self.modules, "")
        if command[:2] == ["caddy", "validate"]:
            self.validated_env.append(kwargs.get("env"))
        return super()._run(command, **kwargs)


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "opt"
    (root / "config").mkdir(parents=True)
    write_json(root / "config/installation.json", {**HOSTING, "root": str(root)})
    write_environment(root / "config/app.env", {"INSTANCE_URL_SUFFIX": "hosting"})
    system = CaddyHost(tmp_path / "host")
    return Manager(root, system=system), system


def proxy(manager: Manager, *argv: str):
    args = ops_cli.parser().parse_args(["--root", str(manager.root), "proxy", *argv])
    return ops_cli.configure_proxy(manager, args)


def test_proxy_install_remembers_the_certificate_mode(installed):
    manager, system = installed
    result = proxy(manager, "--install", "--cloudflare", "--email", "ops@example.org")
    assert result["tls"] == "acme" and result["cloudflare"] is True
    record = read_json(manager.config_dir / "proxy.json")
    assert record["tls"] == {"mode": "acme", "cloudflare": True} and record["email"] == "ops@example.org"
    assert "disable_tlsalpn_challenge" in system.proxy_file.read_text()
    proxy(manager, "--install")
    assert read_json(manager.config_dir / "proxy.json")["tls"]["cloudflare"] is True
    assert read_json(manager.config_dir / "proxy.json")["email"] == "ops@example.org", "kept without --email"
    proxy(manager, "--install", "--no-cloudflare")
    assert "cert_issuer" not in system.proxy_file.read_text()


def test_proxy_cloudflare_dns_needs_the_module_and_a_token(installed, tmp_path):
    manager, system = installed
    with pytest.raises(ValueError, match="no Cloudflare DNS module"):
        proxy(manager, "--install", "--tls", "cloudflare-dns")
    system.modules = "dns.providers.cloudflare\nhttp.handlers.file_server\n"
    with pytest.raises(ValueError, match="--cloudflare-token-file"):
        proxy(manager, "--install", "--tls", "cloudflare-dns")
    token = tmp_path / "token"
    token.write_text("not a token\n")
    with pytest.raises(ValueError, match="only the token"):
        proxy(manager, "--install", "--tls", "cloudflare-dns", "--cloudflare-token-file", str(token))
    token.write_text(TOKEN + "\n")
    result = proxy(manager, "--install", "--tls", "cloudflare-dns", "--cloudflare-token-file", str(token))
    assert result["tls"] == "cloudflare-dns"
    assert system.validated_env[-1]["CLOUDFLARE_API_TOKEN"] == TOKEN, "validated with the token Caddy will have"
    environment = system.caddy_environment_file
    assert read_environment(environment) == {"CLOUDFLARE_API_TOKEN": TOKEN}
    assert (environment.stat().st_mode & 0o777) == 0o600
    assert f"EnvironmentFile={environment}" in system.caddy_dropin.read_text()
    assert ["systemctl", "daemon-reload"] in system.commands
    assert system.commands[-1] == ["systemctl", "restart", "caddy"], "{env.*} needs a restart"
    assert TOKEN not in system.proxy_file.read_text()
    assert TOKEN not in (manager.config_dir / "proxy.json").read_text()
    # Later runs (and updates) validate with the stored token and only reload.
    proxy(manager, "--install")
    assert system.validated_env[-1]["CLOUDFLARE_API_TOKEN"] == TOKEN
    assert system.commands[-1] == ["systemctl", "reload-or-restart", "caddy"]
    assert "dns cloudflare" in system.proxy_file.read_text()


def test_proxy_cloudflare_dns_needs_a_recent_caddy(installed, tmp_path):
    manager, system = installed
    system.version = "v2.8.4 h1:x"
    with pytest.raises(ValueError, match="2.10"):
        proxy(manager, "--tls", "cloudflare-dns")


def test_proxy_origin_certificate_is_copied_for_caddy(installed, tmp_path):
    manager, system = installed
    certificate, key = tmp_path / "c.pem", tmp_path / "k.pem"
    certificate.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    with pytest.raises(ValueError, match="--origin-cert"):
        proxy(manager, "--install", "--tls", "origin-cert")
    with pytest.raises(ValueError, match="both"):
        proxy(manager, "--install", "--tls", "origin-cert", "--origin-cert", str(certificate))
    proxy(manager, "--install", "--tls", "origin-cert", "--origin-cert", str(certificate), "--origin-key", str(key))
    installed_key = system.proxy_file.parent / "bananawiki-origin.key"
    assert installed_key.read_text() == key.read_text() and (installed_key.stat().st_mode & 0o077) in (0, 0o040)
    assert f"tls {system.proxy_file.parent / 'bananawiki-origin.crt'} {installed_key}" in system.proxy_file.read_text()
    assert read_json(manager.config_dir / "proxy.json")["tls"]["origin_key"] == str(installed_key)
    proxy(manager, "--install")  # remembered
    assert "bananawiki-origin.crt" in system.proxy_file.read_text()
    key.write_text("not pem")
    with pytest.raises(ValueError, match="PEM"):
        proxy(manager, "--install", "--origin-cert", str(certificate), "--origin-key", str(key))


def test_proxy_warns_about_nested_hosts_and_old_caddy(installed, capsys):
    manager, system = installed
    write_json(manager.config_dir / "installation.json",
               {**HOSTING, "root": str(manager.root), "domain": "hosting.example.org",
                "portal_domain": "portal.hosting.example.org"})
    system.version = "v2.6.2 h1:x"
    assert proxy(manager) is None
    captured = capsys.readouterr()
    assert "{client_ip}" not in captured.out
    assert "ERR_SSL_VERSION_OR_CIPHER_MISMATCH" in captured.err and "2.7 or newer" in captured.err
    result = proxy(manager, "--install")
    assert len(result["warnings"]) == 3


def test_updates_re_render_the_saved_mode(installed):
    manager, system = installed
    proxy(manager, "--install", "--cloudflare")
    features = manager.features
    manager.features = lambda revision: type("F", (), {"runtime_agent": True})()  # type: ignore[method-assign]
    try:
        system.proxy_file.write_text(system.proxy_file.read_text())  # unchanged, still ours
        text, warning = manager.proxy_plan({**manager.settings(), "revision": "r"})
    finally:
        manager.features = features  # type: ignore[method-assign]
    assert warning is None and text is None, "the saved mode renders the same file"
