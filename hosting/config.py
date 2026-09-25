"""Configuration for the BananaWiki managed hosting platform."""

import os
import re
import secrets
import tempfile

# BananaWiki root: parent of this hosting/ directory
BW_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Portal server
HOSTING_PORT = int(os.environ.get("HOSTING_PORT", "5099"))
HOSTING_HOST = os.environ.get("HOSTING_HOST", "127.0.0.1")
HOSTING_PROXY_MODE = os.environ.get("HOSTING_PROXY_MODE", "0").strip().lower() not in ("0", "false", "no", "")
HOSTING_PREFERRED_URL_SCHEME = os.environ.get("HOSTING_PREFERRED_URL_SCHEME", "").strip() or ""

# Password hash method for hosting portal accounts and seeded wiki instances.
# ``auto`` uses scrypt where Python/OpenSSL supports it and pbkdf2:sha256
# elsewhere, keeping Linux production defaults while allowing macOS/Windows
# development runtimes without hashlib.scrypt.
PASSWORD_HASH_METHOD = os.environ.get(
    "BW_PASSWORD_HASH_METHOD",
    os.environ.get("HASH_METHOD", "auto"),
)

# Secret key: prefer env var, then persistent file, then generate one.
# The path can be overridden via HOSTING_SECRET_KEY_PATH to support read-only
# deployments where the install tree is not writable.
_SECRET_KEY_PATH = os.environ.get(
    "HOSTING_SECRET_KEY_PATH",
    os.path.join(os.path.dirname(__file__), "data", ".secret_key"),
)


def _load_or_generate_secret_key():
    """Load the secret key from file or generate one on first run.

    Uses atomic write (temp file + ``os.replace``) when generating a new
    key so that concurrent processes never see a partially-written file.
    """
    env = os.environ.get("HOSTING_SECRET_KEY")
    if env:
        return env
    from filelock import FileLock
    os.makedirs(os.path.dirname(_SECRET_KEY_PATH), mode=0o700, exist_ok=True)
    with FileLock(_SECRET_KEY_PATH + ".lock", timeout=20, mode=0o600):
        try:
            with open(_SECRET_KEY_PATH) as f:
                key = f.read(4097).strip()
            if len(key) > 4096 or not key:
                raise ValueError("The hosting key file is empty or invalid; restore its original key")
            if key:
                return key
        except FileNotFoundError:
            pass
        key = secrets.token_hex(32)
        os.makedirs(os.path.dirname(_SECRET_KEY_PATH), exist_ok=True)
        # Atomic write: create a temp file and rename it so concurrent readers
        # never see a partial key.
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(_SECRET_KEY_PATH), prefix=".secret_key_"
        )
        try:
            os.write(fd, key.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, _SECRET_KEY_PATH)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return key


HOSTING_SECRET_KEY = _load_or_generate_secret_key()

# One-time gate for the very first platform-admin signup.  Production refuses
# to expose the bootstrap form without this value; installers generate it and
# print the tokenized local URL.  It is ignored as soon as an admin exists.
HOSTING_BOOTSTRAP_TOKEN = os.environ.get("HOSTING_BOOTSTRAP_TOKEN", "").strip()

# Transactional email.  Supported providers deliberately use the standard
# library so a beta deployment does not gain a large SDK dependency.
HOSTING_EMAIL_PROVIDER = os.environ.get("HOSTING_EMAIL_PROVIDER", "").strip().lower()
HOSTING_EMAIL_API_KEY = os.environ.get("HOSTING_EMAIL_API_KEY", "").strip()
HOSTING_EMAIL_FROM = os.environ.get("HOSTING_EMAIL_FROM", "").strip()
HOSTING_EMAIL_REPLY_TO = os.environ.get("HOSTING_EMAIL_REPLY_TO", "").strip()
HOSTING_EMAIL_SMTP_HOST = os.environ.get("HOSTING_EMAIL_SMTP_HOST", "").strip()
HOSTING_EMAIL_SMTP_PORT = int(os.environ.get("HOSTING_EMAIL_SMTP_PORT", "587"))
HOSTING_EMAIL_SMTP_USERNAME = os.environ.get("HOSTING_EMAIL_SMTP_USERNAME", "").strip()
HOSTING_EMAIL_SMTP_PASSWORD = os.environ.get("HOSTING_EMAIL_SMTP_PASSWORD", "")
HOSTING_EMAIL_SMTP_TLS = os.environ.get("HOSTING_EMAIL_SMTP_TLS", "1").strip().lower() not in ("0", "false", "no")
HOSTING_EMAIL_TOKEN_TTL_SECONDS = max(300, int(os.environ.get("HOSTING_EMAIL_TOKEN_TTL_SECONDS", "86400")))
# Daily send limit to stay within free-tier quotas (0 = unlimited).
# Brevo free = 300/day, Resend free = 100/day.
HOSTING_EMAIL_DAILY_LIMIT = int(os.environ.get("HOSTING_EMAIL_DAILY_LIMIT", "300"))

# Database: separate from any BananaWiki instance
HOSTING_DATABASE_PATH = os.environ.get(
    "HOSTING_DATABASE_PATH",
    os.path.join(os.path.dirname(__file__), "data", "hosting.db"),
)

HOSTING_BACKUP_KEY_PATH = os.environ.get(
    "HOSTING_BACKUP_KEY_PATH",
    os.path.join(os.path.dirname(HOSTING_DATABASE_PATH), ".backup_encryption_key"),
)


def load_or_generate_backup_key():
    """Return the 256-bit backup encryption key, creating it with mode 0600."""
    env = os.environ.get("HOSTING_BACKUP_ENCRYPTION_KEY", "").strip()
    if env:
        import base64
        try:
            raw = base64.urlsafe_b64decode(env + "=" * (-len(env) % 4))
        except Exception as exc:
            raise RuntimeError("HOSTING_BACKUP_ENCRYPTION_KEY is not valid base64") from exc
        if len(raw) != 32:
            raise RuntimeError("HOSTING_BACKUP_ENCRYPTION_KEY must decode to 32 bytes")
        return raw
    from filelock import FileLock
    os.makedirs(os.path.dirname(HOSTING_BACKUP_KEY_PATH), mode=0o700, exist_ok=True)
    with FileLock(HOSTING_BACKUP_KEY_PATH + ".lock", timeout=20):
        try:
            with open(HOSTING_BACKUP_KEY_PATH, "rb") as fh:
                raw = fh.read()
            if len(raw) == 32:
                return raw
            raise RuntimeError("Backup encryption key file must contain exactly 32 bytes")
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(HOSTING_BACKUP_KEY_PATH), exist_ok=True)
        raw = secrets.token_bytes(32)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(HOSTING_BACKUP_KEY_PATH), prefix=".backup_key_"
        )
        try:
            os.write(fd, raw)
            os.close(fd)
            fd = -1
            if os.name != "nt":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, HOSTING_BACKUP_KEY_PATH)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return raw


INSTANCES_DIR = os.environ.get(
    "INSTANCES_DIR",
    os.path.join(os.path.dirname(__file__), "data", "instances"),
)

# Host-owned state for each instance that must stay out of the tenant's reach:
# the plugin safety snapshots the platform takes and the plugin quarantine
# marker.  A tenant container mounts only INSTANCES_DIR/<subdomain> at /data
# and can rewrite anything in there, so these live next to the hosting
# database instead, and must never be inside INSTANCES_DIR.  Back this
# directory up with the hosting database.
HOSTING_PLATFORM_STATE_DIR = os.environ.get(
    "HOSTING_PLATFORM_STATE_DIR",
    os.path.join(os.path.dirname(HOSTING_DATABASE_PATH), "platform_state"),
)
_state_real = os.path.realpath(HOSTING_PLATFORM_STATE_DIR)
_instances_real = os.path.realpath(INSTANCES_DIR)
if _state_real == _instances_real or _state_real.startswith(_instances_real + os.sep):
    raise RuntimeError(
        "HOSTING_PLATFORM_STATE_DIR must not be inside INSTANCES_DIR, where "
        "tenants can write."
    )
del _state_real, _instances_real

# Base domain for instance subdomains (e.g. "example.com").
# Instances are accessible at ``<slug>-hosting.BASE_DOMAIN`` (e.g.
# ``luca-hosting.example.com``).  The ``-hosting`` suffix is the default
# ``INSTANCE_URL_SUFFIX``.
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "")

# Portal domain: where the hosting portal itself is accessible.
#
# By default this is the same as BASE_DOMAIN.  The recommended setup is:
#   BASE_DOMAIN=example.com
#   PORTAL_DOMAIN=hosting.example.com
#   INSTANCE_URL_SUFFIX=hosting  (default)
#
# This gives:
#   portal   → hosting.example.com        (covered by *.example.com ✓)
#   instance → luca-hosting.example.com   (covered by *.example.com ✓)
#
# On **Cloudflare free plan**, Universal SSL only covers one level of
# subdomain (``*.example.com``).  The default ``-hosting`` suffix keeps
# all instance URLs flat (single-level) for Cloudflare compatibility.
PORTAL_DOMAIN = os.environ.get("PORTAL_DOMAIN", "")
_INSTANCE_URL_SUFFIX_RAW = os.environ.get("INSTANCE_URL_SUFFIX")
INSTANCE_URL_SUFFIX = (_INSTANCE_URL_SUFFIX_RAW or "").strip().lower()

# Hosting mode: auto-detect or explicit
# Auto-detection:
#   - If BASE_DOMAIN is a valid domain name → "subdomain" mode
#   - If BASE_DOMAIN is empty or an IP address → "port" mode
#
# "subdomain", each instance gets a subdomain:
#     https://myteam-hosting.example.com
#     Requires a wildcard DNS record and nginx/reverse-proxy.
#
# "port", each instance is exposed on a unique port on the host IP:
#     http://203.0.113.10:6000
#     No domain or reverse-proxy required.  Simpler setup for
#     home-servers, LAN hosts, or quick trials.

_IP_PATTERN = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
)
_IPV6_PATTERN = re.compile(r"^\[?[0-9a-fA-F:]+\]?$")


def _normalize_domain_layout(base_domain, portal_domain, instance_url_suffix, auto_flatten=True):
    """Normalize domain settings for Cloudflare-friendly flat instance hosts.

    When ``auto_flatten`` is enabled and operators set only ``BASE_DOMAIN``
    to a nested host like ``hosting.example.com`` while leaving
    ``PORTAL_DOMAIN`` and ``INSTANCE_URL_SUFFIX`` empty, reinterpret this as:

    - ``BASE_DOMAIN=example.com``
    - ``PORTAL_DOMAIN=hosting.example.com``
    - ``INSTANCE_URL_SUFFIX=hosting``

    This changes instance URLs from ``name.hosting.example.com`` to
    ``name-hosting.example.com`` while preserving the existing portal host.
    """
    base = (base_domain or "").strip().lower()
    portal = (portal_domain or "").strip().lower()
    suffix = (instance_url_suffix or "").strip().lower()

    if not auto_flatten:
        return base, portal, suffix

    if not base or portal or suffix:
        return base, portal, suffix

    if _IP_PATTERN.match(base) or _IPV6_PATTERN.match(base):
        return base, portal, suffix

    if base in ("localhost",):
        return base, portal, suffix

    labels = [label for label in base.split(".") if label]
    # Only auto-normalize domains that are at least third-level.
    if len(labels) < 3:
        return base, portal, suffix

    prefix = labels[0]
    root = ".".join(labels[1:])
    if not prefix or not root:
        return base, portal, suffix

    return root, base, prefix


# Enable automatic flattening by default to prevent SSL errors on Cloudflare
# free plans (which only cover single-level wildcards).
#
# When a nested BASE_DOMAIN is provided (e.g. hosting.example.com), this
# automatically reinterprets it as BASE_DOMAIN=example.com and
# INSTANCE_URL_SUFFIX=hosting, producing luca-hosting.example.com URLs.
AUTO_FLATTEN_DOMAIN_LAYOUT = os.environ.get(
    "HOSTING_AUTO_FLATTEN_DOMAIN_LAYOUT", "1"
).strip().lower() not in ("", "0", "false", "no")

BASE_DOMAIN, PORTAL_DOMAIN, INSTANCE_URL_SUFFIX = _normalize_domain_layout(
    BASE_DOMAIN,
    PORTAL_DOMAIN,
    INSTANCE_URL_SUFFIX,
    auto_flatten=AUTO_FLATTEN_DOMAIN_LAYOUT,
)

# Default INSTANCE_URL_SUFFIX to "hosting" so instances always use the
# flat {slug}-hosting.{BASE_DOMAIN} format (e.g. myteam-hosting.example.com).
# Applied after auto-flatten so that _normalize_domain_layout still sees the
# raw (possibly empty) suffix and can set it via flattening when needed.
#
# Operators can opt out by setting INSTANCE_URL_SUFFIX to an empty value
# in the environment (e.g. INSTANCE_URL_SUFFIX= ), which produces the
# classic {slug}.{BASE_DOMAIN} format.
if _INSTANCE_URL_SUFFIX_RAW is None:
    INSTANCE_URL_SUFFIX = "hosting"

def _detect_hosting_mode():
    """Auto-detect hosting mode from BASE_DOMAIN and environment.

    Returns ``"subdomain"`` when BASE_DOMAIN is a proper domain name,
    ``"port"`` when it looks like an IP or is not set.
    Can be overridden with the ``HOSTING_MODE`` env var.
    """
    explicit = os.environ.get("HOSTING_MODE", "").strip().lower()
    if explicit:
        if explicit not in ("subdomain", "port", "onion"):
            raise ValueError(
                f"HOSTING_MODE must be 'subdomain', 'port', or 'onion', got '{explicit}'"
            )
        return explicit

    domain = BASE_DOMAIN.strip()
    if not domain:
        return "port"
    if _IP_PATTERN.match(domain) or _IPV6_PATTERN.match(domain):
        return "port"
    if domain in ("localhost", "127.0.0.1", "0.0.0.0"):
        return "port"
    # Must have at least one dot and look like a domain name (not just digits
    # and dots like an IP).  Reject bare hostnames without dots.
    if "." in domain and not _IP_PATTERN.match(domain):
        return "subdomain"
    return "port"


HOSTING_MODE = _detect_hosting_mode()

# Where the portal, its emails and its help centre tell people to write. It
# used to be written into forty-odd templates and messages as the address of
# one deployment, so every other operator's users were told to email that
# deployment's inbox. Set HOSTING_CONTACT_EMAIL, or it falls back to the
# address in HOSTING_EMAIL_REPLY_TO, then to contact@ at BASE_DOMAIN when the
# platform runs on a real domain. Port and onion deployments have no domain to
# guess from, so they get a placeholder and a startup warning instead.
CONTACT_EMAIL_PLACEHOLDER = "contact@localhost"


def _contact_email():
    from email.utils import parseaddr
    explicit = os.environ.get("HOSTING_CONTACT_EMAIL", "").strip()
    if explicit:
        address = parseaddr(explicit)[1]
        if "@" not in address or any(c in address for c in "<>\"' \n\r"):
            raise ValueError("HOSTING_CONTACT_EMAIL must be an email address")
        return address
    reply_to = parseaddr(HOSTING_EMAIL_REPLY_TO)[1]
    if "@" in reply_to:
        return reply_to
    if HOSTING_MODE == "subdomain" and BASE_DOMAIN:
        return f"contact@{BASE_DOMAIN}"
    return CONTACT_EMAIL_PLACEHOLDER


HOSTING_CONTACT_EMAIL = _contact_email()

# Optional public status page, linked from transactional emails when set.
def _status_url(value):
    from urllib.parse import urlsplit
    value = (value or "").strip()
    if value:
        parts = urlsplit(value)
        # Any userinfo at all is refused, including ":password@" with no user.
        if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
            raise ValueError("HOSTING_STATUS_URL must be an absolute HTTP(S) URL without credentials")
    return value


HOSTING_STATUS_URL = _status_url(os.environ.get("HOSTING_STATUS_URL", ""))




def _effective_portal_domain():
    """Return the domain the hosting portal listens on.

    If ``PORTAL_DOMAIN`` is explicitly set, use that.  Otherwise fall back
    to ``BASE_DOMAIN`` (the traditional single-domain setup where the
    portal and instances share the same parent).
    """
    pd = PORTAL_DOMAIN.strip().lower()
    return pd if pd else BASE_DOMAIN.strip().lower()


EFFECTIVE_PORTAL_DOMAIN = _effective_portal_domain()

# Public hostname or IP address used to build instance URLs in port mode.
# Ignored in subdomain mode (where BASE_DOMAIN is used instead).
# Examples: "203.0.113.10", "myserver.local", "localhost"
#
# When not explicitly set, the platform attempts to auto-detect the machine's
# public IP by querying well-known IP-echo services (reliable on cloud VMs
# behind NAT).  Falls back to the outbound interface address, then "localhost".

def _is_private_host(host: str) -> bool:
    """Return ``True`` if *host* is a non-public address.

    Covers localhost names, loopback, unspecified, the RFC 1918 private
    IP ranges (10/8, 172.16-31/12, 192.168/16), and IPv6 private ranges
    (link-local fe80::/10 and unique-local fc00::/7).
    """
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ""):
        return True
    # Check IPv6 addresses using the ipaddress module (handles all edge cases).
    # Strip square brackets from IPv6 literals (e.g. "[::1]").
    addr_str = host.strip("[]")
    if ":" in addr_str:
        import ipaddress
        try:
            addr = ipaddress.ip_address(addr_str)
            return (
                addr.is_loopback
                or addr.is_link_local
                or addr.is_private
                or addr.is_unspecified
            )
        except ValueError:
            pass
        return False
    # IPv4 private ranges
    parts = host.split(".")
    if len(parts) == 4:
        try:
            first, second = int(parts[0]), int(parts[1])
            if first == 10:
                return True
            if first == 172 and 16 <= second <= 31:
                return True
            if first == 192 and second == 168:
                return True
        except ValueError:
            pass
    return False


def _detect_public_host() -> str:
    """Auto-detect the machine's public IP for port-mode instance URLs.

    Only called when ``HOSTING_PUBLIC_HOST`` is not set *and* the
    deployment is in port mode.  In subdomain / apex mode the public
    host is the ``BASE_DOMAIN`` value, so the detection is skipped
    entirely to avoid blocking Gunicorn startup on external HTTP calls
    in network-restricted or air-gapped environments.

    Queries well-known IP-echo services first so that the public IP is
    returned even when the machine is behind NAT (common on cloud VMs).
    Falls back to the outbound interface address, then ``"localhost"``.
    """
    import socket
    import urllib.request

    # Try external IP-echo services. These return the public IP even on
    # cloud VMs where the public address is NAT'd and not locally visible.
    for url in (
        "https://checkip.amazonaws.com",
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me",
        "https://ipinfo.io/ip",
    ):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                ip = resp.read().decode().strip()
                if ip and _IP_PATTERN.match(ip):
                    return ip
        except Exception:
            continue

    # Fall back to the outbound interface IP.  On machines with a directly
    # attached public IP this gives the right answer; on NAT'd VMs it will
    # return the private address: in that case we cannot reliably determine
    # the public IP without a working network path to an IP-echo service.
    # Use DNS ports (53) which are more universally open than HTTP ports.
    for target, family in [
        (("1.1.1.1", 53), socket.AF_INET),
        (("8.8.8.8", 53), socket.AF_INET),
    ]:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect(target)
                ip = s.getsockname()[0]
                if ip and not _is_private_host(ip):
                    return ip
        except OSError:
            continue

    # Try IPv6 if no IPv4 address was found (IPv6-only VPS).
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect(("2001:4860:4860::8888", 53))
            ip = s.getsockname()[0]
            if ip and not _is_private_host(ip):
                return ip
    except OSError:
        pass

    # All detection methods failed or returned a private address.  Return
    # "localhost" so that the warning in app.py fires and the operator
    # knows to set HOSTING_PUBLIC_HOST to their server's public IP.
    return "localhost"


def _resolve_public_host() -> str:
    """Return HOSTING_PUBLIC_HOST, running auto-detection only in port mode.

    In subdomain/apex mode ``BASE_DOMAIN`` is the public host; the
    external HTTP detection calls are an unnecessary startup cost and
    will fail completely in air-gapped deployments.  Skip them by
    returning the explicit env value (or an empty string that app.py
    can warn about) without making any outbound network connections.
    """
    explicit = os.environ.get("HOSTING_PUBLIC_HOST", "").strip()
    if explicit:
        return explicit
    # Lazy mode detection: BASE_DOMAIN set + not a bare IP → subdomain mode.
    # We cannot call _detect_hosting_mode() here (it hasn't run yet), so we
    # replicate the core heuristic: an empty or IP-address BASE_DOMAIN means
    # port mode; a proper domain name means subdomain/apex mode.
    bd = os.environ.get("HOSTING_BASE_DOMAIN", os.environ.get("BASE_DOMAIN", "")).strip()
    if bd and not _IP_PATTERN.match(bd):
        # Subdomain / apex mode: public host is the domain itself.
        return bd
    # Port mode or no domain configured: run the full detection.
    return _detect_public_host()


HOSTING_PUBLIC_HOST = _resolve_public_host()

# URL scheme for instance URLs in port mode ("http" or "https").
# Instance processes serve plain HTTP; TLS termination belongs at the proxy.
# Default is "https": operators running plain HTTP in port mode should set
# HOSTING_PUBLIC_SCHEME=http explicitly.
HOSTING_PUBLIC_SCHEME = os.environ.get("HOSTING_PUBLIC_SCHEME", "https")

# Starting port range for instance processes.
# Port 6000 is traditionally reserved for the X11 display server and is
# frequently unavailable or blocked by firewalls, so the range starts at 6001.
INSTANCE_PORT_START = int(os.environ.get("INSTANCE_PORT_START", "6001"))
INSTANCE_PORT_END = int(os.environ.get("INSTANCE_PORT_END", "7000"))

# Resource limits: tunable via environment without code deploy
MAX_INSTANCES_PER_ACCOUNT = int(os.environ.get("MAX_INSTANCES_PER_ACCOUNT", "5"))
INSTANCE_DURATION_DAYS = int(os.environ.get("INSTANCE_DURATION_DAYS", "14"))
INSTANCE_STORAGE_LIMIT_MB = int(os.environ.get("INSTANCE_STORAGE_LIMIT_MB", "500"))
INSTANCE_MAX_UPLOAD_MB = int(os.environ.get("INSTANCE_MAX_UPLOAD_MB", "16"))
INSTANCE_MAX_ATTACHMENT_MB = int(os.environ.get("INSTANCE_MAX_ATTACHMENT_MB", "100"))
INSTANCE_MEMORY_LIMIT_MB = int(os.environ.get("INSTANCE_MEMORY_LIMIT_MB", "768"))
INSTANCE_NOFILE_LIMIT = int(os.environ.get("INSTANCE_NOFILE_LIMIT", "1024"))
INSTANCE_CPU_LIMIT = os.environ.get("INSTANCE_CPU_LIMIT", "1.0").strip() or "1.0"
INSTANCE_PIDS_LIMIT = int(os.environ.get("INSTANCE_PIDS_LIMIT", "256"))
INSTANCE_STARTUP_TIMEOUT_SECONDS = max(30, min(600, int(
    os.environ.get("INSTANCE_STARTUP_TIMEOUT_SECONDS", "120")
)))

# Tenant runtime.  ``process`` preserves the lightweight self-hosted setup.
# Public hosting that permits custom Python plugins must use ``docker`` so a
# plugin is contained inside its tenant rather than imported by a shared Unix
# process.  Deployment checks reject external plugins when this boundary is
# absent, even if an operator accidentally sets BW_ALLOW_EXTERNAL_PLUGINS=1.
HOSTING_INSTANCE_RUNTIME = os.environ.get(
    "HOSTING_INSTANCE_RUNTIME", "docker"
).strip().lower()
if HOSTING_INSTANCE_RUNTIME not in {"process", "docker"}:
    raise RuntimeError("HOSTING_INSTANCE_RUNTIME must be 'process' or 'docker'")

# Tenant administrators may install Python plugins. External networking is a
# host operator decision. Domain-based hosting can use internal bridges;
# Docker does not publish ports for those networks, so legacy port mode
# retains a routed bridge and requires the operator's outbound firewall.
HOSTING_TENANT_NETWORK = os.environ.get(
    "HOSTING_TENANT_NETWORK", "isolated" if HOSTING_MODE == "subdomain" else "outbound"
).strip().lower()
if HOSTING_TENANT_NETWORK not in {"isolated", "outbound"}:
    raise RuntimeError("HOSTING_TENANT_NETWORK must be 'isolated' or 'outbound'")
# Port and onion hosting reach tenants through published ports, and Docker
# publishes nothing from an internal network, so those modes cannot serve
# isolated tenants.
if (
    HOSTING_INSTANCE_RUNTIME == "docker"
    and HOSTING_MODE in ("port", "onion")
    and HOSTING_TENANT_NETWORK == "isolated"
):
    raise RuntimeError(
        "Isolated tenants require subdomain hosting. Configure a domain and "
        "its proxy, or choose HOSTING_TENANT_NETWORK=outbound for port or "
        "onion mode."
    )

# True when tenant containers can reach the network: the Internet, the host's
# LAN and host services listening on the bridge gateway.  That is the default
# in port and onion mode.  The portal logs a warning at startup
# (container_runtime.warn_if_tenant_network_outbound) and the operator is
# expected to add host firewall rules; see docs/deployment.md.
HOSTING_TENANT_NETWORK_OUTBOUND = (
    HOSTING_INSTANCE_RUNTIME == "docker" and HOSTING_TENANT_NETWORK == "outbound"
)

# Comma-separated plugin ids the operator wants kept off every tenant, added
# to the wiki's own list and passed into each tenant as
# BW_MANAGED_PLUGIN_DENYLIST (see hosting/instance_environment.py).  The wiki
# matches ids only, so this is a policy switch, not a security boundary: a
# tenant allowed to run container plugins can upload the same code under
# another id.
HOSTING_TENANT_PLUGIN_DENYLIST = ",".join(
    item.strip()
    for item in os.environ.get("HOSTING_TENANT_PLUGIN_DENYLIST", "").split(",")
    if item.strip()
)
HOSTING_CONTAINER_IMAGE = os.environ.get(
    "HOSTING_CONTAINER_IMAGE", "bananawiki-tenant:1.4.4"
).strip()
HOSTING_CONTAINER_INTERNAL_PORT = int(os.environ.get(
    "HOSTING_CONTAINER_INTERNAL_PORT", "5001"
))

# Instance URLs use the format ``{slug}-{suffix}.{BASE_DOMAIN}`` by default
# (e.g. ``myteam-hosting.example.com``).  The suffix defaults to ``hosting``
# so that all instance subdomains are flat single-level names compatible
# with Cloudflare free SSL and single-level wildcard certificates
# (``*.example.com``).
#
# Example:
#   BASE_DOMAIN=example.com
#   PORTAL_DOMAIN=hosting.example.com
#   INSTANCE_URL_SUFFIX=hosting
#
# Gives:
#   portal   → hosting.example.com          (Cloudflare ✓)
#   instance → myteam-hosting.example.com   (Cloudflare ✓)
#
# The suffix must be lowercase alphanumeric with optional hyphens and must
# not start or end with a hyphen.
# ``INSTANCE_URL_SUFFIX`` is resolved above to allow auto-normalization.

# Subdomain rules
SUBDOMAIN_MIN_LENGTH = int(os.environ.get("SUBDOMAIN_MIN_LENGTH", "3"))
SUBDOMAIN_MAX_LENGTH = int(os.environ.get("SUBDOMAIN_MAX_LENGTH", "40"))
# Reserved subdomains.  Only labels that would genuinely collide with the
# platform's own DNS / mail / portal records are blocked here.  Generic
# content words (``wiki``, ``blog``, ``app``, ``login``…) used to live in
# this set defensively, but the apex-vs-hosting URL split means a hosted
# instance named ``wiki`` resolves to ``wiki-{INSTANCE_URL_SUFFIX}.{BASE_DOMAIN}``:
# a different host from ``wiki.{BASE_DOMAIN}``, so reserving them was
# overzealous and prevented users from claiming their natural slug after
# an admin already owned the apex.
RESERVED_SUBDOMAINS = {
    # DNS / mail infrastructure.
    "www", "ns1", "ns2", "ns3",
    "mail", "smtp", "pop", "imap", "webmail", "email",
    "ftp", "ssh", "vpn",
    # Portal / API / admin routes.
    "portal", "admin", "api",
    # Conventional static-content prefixes.
    "static", "assets", "cdn",
}

# When PORTAL_DOMAIN differs from BASE_DOMAIN, the portal's subdomain
# prefix (e.g. "hosting" from "hosting.example.com" when BASE_DOMAIN is
# "example.com") must be reserved to prevent instance name collisions.
if EFFECTIVE_PORTAL_DOMAIN and BASE_DOMAIN:
    _base_lower = BASE_DOMAIN.strip().lower()
    _portal_lower = EFFECTIVE_PORTAL_DOMAIN.lower()
    _suffix = "." + _base_lower
    if _portal_lower != _base_lower and _portal_lower.endswith(_suffix):
        _portal_prefix = _portal_lower[: -len(_suffix)]
        if _portal_prefix and "." not in _portal_prefix:
            RESERVED_SUBDOMAINS.add(_portal_prefix)

# When INSTANCE_URL_SUFFIX is set, also reserve that word so users cannot
# create a bare instance named the same as the suffix (which would produce
# a confusing ``{suffix}-{suffix}.{BASE_DOMAIN}`` URL).
if INSTANCE_URL_SUFFIX:
    RESERVED_SUBDOMAINS.add(INSTANCE_URL_SUFFIX)

# Administrator and deployment documentation, linked from the help centre.
# The customer-facing help centre itself is built in; see hosting/help_content.py.
PROJECT_DOCS_URL = "https://github.com/BananaSuite/BananaWiki/tree/main/docs"
HOSTING_CUSTOM_DOMAIN_TARGET = os.environ.get("HOSTING_CUSTOM_DOMAIN_TARGET", "").strip().lower().rstrip(".")
if HOSTING_CUSTOM_DOMAIN_TARGET:
    try:
        HOSTING_CUSTOM_DOMAIN_TARGET = HOSTING_CUSTOM_DOMAIN_TARGET.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("HOSTING_CUSTOM_DOMAIN_TARGET must be a DNS hostname") from exc
    _domain_labels = HOSTING_CUSTOM_DOMAIN_TARGET.split(".")
    if len(HOSTING_CUSTOM_DOMAIN_TARGET) > 253 or len(_domain_labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in _domain_labels
    ) or _domain_labels[-1].isdigit():
        raise ValueError("HOSTING_CUSTOM_DOMAIN_TARGET must be a DNS hostname")
HOSTING_CUSTOM_DOMAIN_IPS = tuple(
    value.strip() for value in os.environ.get("HOSTING_CUSTOM_DOMAIN_IPS", "").split(",") if value.strip()
)
if HOSTING_CUSTOM_DOMAIN_IPS:
    import ipaddress
    for _domain_ip in HOSTING_CUSTOM_DOMAIN_IPS:
        ipaddress.ip_address(_domain_ip)
# Admin wiki imports can be much larger than reverse proxies/CDNs allow in a
# single request.  The hosting UI uploads them in chunks below the default
# hosting portal 16 MB Nginx cap (and well below Cloudflare's common 100 MB
# cap), then reassembles the ZIP server-side before handing it to the normal
# importer.
HOSTING_IMPORT_CHUNK_BYTES = int(
    os.environ.get("HOSTING_IMPORT_CHUNK_BYTES", str(8 * 1024 * 1024))
)
HOSTING_IMPORT_MAX_BYTES = int(
    os.environ.get("HOSTING_IMPORT_MAX_BYTES", str(10 * 1024 * 1024 * 1024))
)
HOSTING_IMPORT_MAX_EXTRACTED_BYTES = int(
    os.environ.get(
        "HOSTING_IMPORT_MAX_EXTRACTED_BYTES",
        str(HOSTING_IMPORT_MAX_BYTES * 3),
    )
)
HOSTING_IMPORT_MAX_MEMBERS = int(
    os.environ.get("HOSTING_IMPORT_MAX_MEMBERS", "250000")
)
HOSTING_IMPORT_MIN_FREE_BYTES = int(
    os.environ.get("HOSTING_IMPORT_MIN_FREE_BYTES", str(512 * 1024 * 1024))
)
HOSTING_IMPORT_TEMP_DIR = os.environ.get(
    "HOSTING_IMPORT_TEMP_DIR",
    os.path.join(os.path.dirname(__file__), "data", "tmp_imports"),
)

# Per-instance archive downloads can be very large. Keep temp files in a known
# location so abandoned downloads from killed workers can be swept later.
HOSTING_EXPORT_TEMP_DIR = os.environ.get(
    "HOSTING_EXPORT_TEMP_DIR",
    os.path.join(os.path.dirname(__file__), "data", "tmp_exports"),
)
HOSTING_EXPORT_COMPRESS_LEVEL = int(
    os.environ.get("HOSTING_EXPORT_COMPRESS_LEVEL", "1")
)
HOSTING_EXPORT_STORE_FILE_BYTES = int(
    os.environ.get("HOSTING_EXPORT_STORE_FILE_BYTES", str(64 * 1024 * 1024))
)
HOSTING_EXPORT_STORE_EXTENSIONS = os.environ.get(
    "HOSTING_EXPORT_STORE_EXTENSIONS",
    ",".join([
        ".7z", ".avi", ".bz2", ".dmg", ".docx", ".flac", ".gif", ".gz",
        ".heic", ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4",
        ".ogg", ".pdf", ".png", ".pptx", ".rar", ".tar", ".tgz", ".webm",
        ".webp", ".xlsx", ".xz", ".zip", ".zst",
    ]),
)

SOURCE_CODE_URL = os.environ.get("BW_SOURCE_URL", "https://github.com/BananaSuite/BananaWiki").strip()
