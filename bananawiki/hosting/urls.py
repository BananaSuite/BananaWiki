"""Names and addresses: wiki slugs, data directory names and public URLs."""

from __future__ import annotations

import re
from typing import Any

from flask import current_app, url_for

from . import settings
from .config import HostingConfig, reserved_subdomains, valid_suffix
from .errors import ServiceError

DOMAIN_MODES = ("hosting", "apex")
TERMINATED_MARKER = "--terminated-"
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_DATA_DIR = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:__apex)?$")


def config() -> HostingConfig:
    return current_app.config["HOSTING"]


def instance_suffix() -> str:
    """The suffix in ``<slug>-<suffix>.<BASE_DOMAIN>``; the platform setting wins over the env."""
    if settings.flag("instance_suffix_disabled"):
        return ""
    stored = (settings.get("instance_url_suffix") or "").strip().lower()
    return stored if stored and valid_suffix(stored) else config().instance_url_suffix


def reserved() -> frozenset[str]:
    return reserved_subdomains(config(), instance_suffix())


def validate_slug(value: str, *, account_is_admin: bool, domain_mode: str = "hosting") -> str:
    """Return the normalised slug or raise :class:`ServiceError`."""
    cfg = config()
    slug = (value or "").strip().lower()
    if domain_mode not in DOMAIN_MODES:
        raise ServiceError("hosting.slug.invalid_mode")
    if domain_mode == "apex" and not account_is_admin:
        raise ServiceError("hosting.slug.apex_admin_only")
    if not slug:
        raise ServiceError("hosting.slug.required")
    if len(slug) < cfg.subdomain_min_length:
        raise ServiceError("hosting.slug.too_short", min=cfg.subdomain_min_length)
    if len(slug) > cfg.subdomain_max_length:
        raise ServiceError("hosting.slug.too_long", max=cfg.subdomain_max_length)
    if not _SLUG.match(slug) or "--" in slug:
        raise ServiceError("hosting.slug.invalid_chars")
    if slug in reserved():
        raise ServiceError("hosting.slug.reserved", slug=slug)
    return slug


def data_dir_name(slug: str, domain_mode: str) -> str:
    """``<slug>`` or ``<slug>__apex`` under ``INSTANCES_DIR`` (the 1.4 layout)."""
    name = f"{slug}__apex" if domain_mode == "apex" else slug
    if not _DATA_DIR.match(name):
        raise ValueError(f"Unsafe data directory name: {name!r}")
    return name


def archived_slug(instance_id: str, slug: str) -> str:
    return f"{slug.strip().lower()}{TERMINATED_MARKER}{instance_id[:8].lower()}"


def original_slug(archived: str) -> str:
    return archived.split(TERMINATED_MARKER)[0] if archived and TERMINATED_MARKER in archived else ""


def format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def platform_host(inst: dict[str, Any]) -> str:
    """The public host of a wiki in subdomain mode ('' in port mode)."""
    cfg = config()
    if cfg.hosting_mode != "subdomain" or not cfg.base_domain:
        return ""
    slug = inst["subdomain"]
    if (inst.get("domain_mode") or "hosting") == "apex":
        return f"{slug}.{cfg.base_domain}"
    suffix = instance_suffix()
    return f"{slug}-{suffix}.{cfg.base_domain}" if suffix else f"{slug}.{cfg.base_domain}"


def instance_url(inst: dict[str, Any]) -> str:
    cfg = config()
    if cfg.hosting_mode != "subdomain":
        if inst.get("port") is None:
            return ""
        return f"{cfg.public_scheme}://{format_host(cfg.public_host)}:{inst['port']}"
    host = platform_host(inst)
    return f"https://{host}" if host else ""


def portal_base_url() -> str:
    """Public origin of the portal, from configuration only (never the request Host)."""
    cfg = config()
    if cfg.hosting_mode == "subdomain" and cfg.effective_portal_domain:
        return f"https://{cfg.effective_portal_domain}"
    host = format_host(cfg.public_host)
    if cfg.port not in (80, 443):
        host = f"{host}:{cfg.port}"
    return f"{cfg.public_scheme}://{host}"


def external_url(endpoint: str, **values: Any) -> str:
    """Absolute link for emails and OAuth, built from :func:`portal_base_url`."""
    return portal_base_url() + url_for(endpoint, _external=False, **values)
