"""Configuration combinations the portal refuses at startup."""

from __future__ import annotations

import pytest

from bananawiki.core.env import ConfigError
from bananawiki.hosting.config import load_config

from .hosting_support import portal_environ

SECRET = "config-test-secret-" + "k" * 32


@pytest.mark.parametrize("mode", ["port", "onion"])
def test_tenant_plugins_are_refused_when_portal_and_wikis_share_a_host(tmp_path, mode):
    environ = portal_environ(tmp_path, BASE_DOMAIN="", HOSTING_MODE=mode, HOSTING_ALLOW_TENANT_PLUGINS="1")
    with pytest.raises(ConfigError, match="HOSTING_ALLOW_TENANT_PLUGINS=1 requires subdomain hosting"):
        load_config(environ, secret_key=SECRET)


@pytest.mark.parametrize("base_domain", ["", "localhost", "192.0.2.10"])
def test_tenant_plugins_are_refused_when_port_mode_is_detected(tmp_path, base_domain):
    detected = load_config(portal_environ(tmp_path, BASE_DOMAIN=base_domain), secret_key=SECRET)
    assert detected.hosting_mode == "port"
    environ = portal_environ(tmp_path, BASE_DOMAIN=base_domain, HOSTING_ALLOW_TENANT_PLUGINS="1")
    with pytest.raises(ConfigError, match="requires subdomain hosting"):
        load_config(environ, secret_key=SECRET)


def test_tenant_plugins_are_refused_when_enabled_by_override(tmp_path):
    environ = portal_environ(tmp_path, BASE_DOMAIN="", HOSTING_MODE="port")
    with pytest.raises(ConfigError, match="requires subdomain hosting"):
        load_config(environ, secret_key=SECRET, allow_tenant_plugins=True)


@pytest.mark.parametrize("mode", ["port", "onion"])
def test_port_and_onion_mode_still_start_without_tenant_plugins(tmp_path, mode):
    cfg = load_config(portal_environ(tmp_path, BASE_DOMAIN="", HOSTING_MODE=mode), secret_key=SECRET)
    assert cfg.hosting_mode == mode and not cfg.allow_tenant_plugins


def test_subdomain_mode_keeps_the_tenant_plugin_opt_in(tmp_path):
    cfg = load_config(portal_environ(tmp_path, HOSTING_ALLOW_TENANT_PLUGINS="1"), secret_key=SECRET)
    assert cfg.hosting_mode == "subdomain" and cfg.allow_tenant_plugins
