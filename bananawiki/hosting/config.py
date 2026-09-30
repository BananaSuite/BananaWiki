"""Hosting portal configuration, read once from the environment.

Every variable BananaWiki Hosting 1.4 honoured is still honoured with the same
meaning and default, so an existing ``config/app.env`` keeps working after the
upgrade. Paths default to ``<repository>/hosting/data`` like 1.4; managed
installations set them explicitly to the writable ``data/`` directory.

Differences from 1.4: when ``HOSTING_PUBLIC_HOST`` is unset in port mode the
portal no longer asks third-party IP-echo services for its address (it uses
the outbound interface address, which sends no traffic), and nothing mutates
the configuration at run time; the instance URL suffix chosen in the platform
settings is applied by :mod:`bananawiki.hosting.urls`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import socket
import tempfile
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from ..core.env import ConfigError, Env

log = logging.getLogger("bananawiki.hosting.config")

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
LEGACY_DATA_DIR = REPO_ROOT / "hosting" / "data"
DEFAULT_SOURCE_URL = "https://github.com/OverloadedTech/BananaWiki"
CONTACT_EMAIL_PLACEHOLDER = "contact@localhost"
HOSTING_MODES = frozenset({"subdomain", "port", "onion"})

_IPV4 = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$")
_IPV6 = re.compile(r"^\[?[0-9a-fA-F:]+\]?$")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SUFFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# Labels that would collide with the platform's own DNS, mail or portal records.
BASE_RESERVED_SUBDOMAINS = frozenset({
    "www", "ns1", "ns2", "ns3", "mail", "smtp", "pop", "imap", "webmail", "email",
    "ftp", "ssh", "vpn", "portal", "admin", "api", "static", "assets", "cdn",
})

_DEFAULT_STORE_EXTENSIONS = (
    ".7z,.avi,.bz2,.dmg,.docx,.flac,.gif,.gz,.heic,.jpeg,.jpg,.m4a,.mkv,.mov,.mp3,.mp4,"
    ".ogg,.pdf,.png,.pptx,.rar,.tar,.tgz,.webm,.webp,.xlsx,.xz,.zip,.zst"
)


@dataclass(frozen=True)
class EmailSettings:
    provider: str
    api_key: str = field(repr=False)
    sender: str
    reply_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str = field(repr=False)
    smtp_tls: bool
    token_ttl_seconds: int
    daily_limit: int


@dataclass(frozen=True)
class InstanceLimits:
    max_per_account: int
    duration_days: int
    storage_limit_mb: int
    max_upload_mb: int
    max_attachment_mb: int
    memory_limit_mb: int
    nofile_limit: int
    cpu_limit: str
    pids_limit: int
    startup_timeout_seconds: int


@dataclass(frozen=True)
class ArchiveSettings:
    import_chunk_bytes: int
    import_max_bytes: int
    import_max_extracted_bytes: int
    import_max_members: int
    import_min_free_bytes: int
    import_temp_dir: str
    export_temp_dir: str
    export_compress_level: int
    export_store_file_bytes: int
    export_store_extensions: tuple[str, ...]


@dataclass(frozen=True)
class HostingConfig:
    env: str
    host: str
    port: int
    proxy_mode: bool
    preferred_url_scheme: str
    secret_key: str = field(repr=False)
    bootstrap_token: str = field(repr=False)
    password_hash_method: str
    database_path: str
    backup_key_path: str
    instances_dir: str
    platform_state_dir: str
    base_domain: str
    portal_domain: str
    instance_url_suffix: str
    hosting_mode: str
    public_host: str
    public_scheme: str
    port_range: tuple[int, int]
    limits: InstanceLimits
    instance_runtime: str
    tenant_network: str
    tenant_plugin_denylist: tuple[str, ...]
    container_image: str
    container_internal_port: int
    subdomain_min_length: int
    subdomain_max_length: int
    custom_domain_target: str
    custom_domain_ips: tuple[str, ...]
    federation_instances: frozenset[str]
    email: EmailSettings
    contact_email: str
    status_url: str
    source_url: str
    archives: ArchiveSettings
    default_signup_mode: str
    maintenance_file: str
    log_level: str
    db_busy_timeout_ms: int = 5000
    min_form_seconds: float = 1.5
    testing: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def effective_portal_domain(self) -> str:
        return self.portal_domain or self.base_domain

    @property
    def tenant_network_outbound(self) -> bool:
        return self.instance_runtime == "docker" and self.tenant_network == "outbound"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _lax_bool(env: Env, name: str, default: str, falsy: tuple[str, ...] = ("0", "false", "no", "off", "")) -> bool:
    """1.4 read these flags as "anything but a false word is on"; keep that."""
    return env.str(name, default).lower() not in falsy


def is_ip_address(value: str) -> bool:
    return bool(_IPV4.match(value) or _IPV6.match(value))


def normalize_domain_layout(base: str, portal: str, suffix: str, *, auto_flatten: bool) -> tuple[str, str, str]:
    """Turn a nested ``BASE_DOMAIN`` (``hosting.example.com``) into the flat layout.

    With only ``BASE_DOMAIN=hosting.example.com`` set, 1.4 reinterpreted it as
    ``BASE_DOMAIN=example.com``, ``PORTAL_DOMAIN=hosting.example.com`` and
    ``INSTANCE_URL_SUFFIX=hosting`` so that wiki hosts stay single-level
    (``team-hosting.example.com``), which Cloudflare's free certificates cover.
    """
    base, portal, suffix = base.strip().lower(), portal.strip().lower(), suffix.strip().lower()
    if not auto_flatten or not base or portal or suffix or is_ip_address(base) or base == "localhost":
        return base, portal, suffix
    labels = [label for label in base.split(".") if label]
    if len(labels) < 3:
        return base, portal, suffix
    return ".".join(labels[1:]), base, labels[0]


def _detect_hosting_mode(env: Env, base_domain: str) -> str:
    explicit = env.str("HOSTING_MODE").lower()
    if explicit:
        if explicit not in HOSTING_MODES:
            raise ConfigError("HOSTING_MODE must be 'subdomain', 'port' or 'onion'.")
        return explicit
    if not base_domain or is_ip_address(base_domain) or base_domain in ("localhost", "0.0.0.0"):
        return "port"
    return "subdomain" if "." in base_domain else "port"


def _outbound_address() -> str:
    """The address of the interface used for outbound traffic (no packet is sent)."""
    for family, target in ((socket.AF_INET, ("192.0.2.1", 53)), (socket.AF_INET6, ("2001:db8::1", 53))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                return probe.getsockname()[0]
        except OSError:
            continue
    return "localhost"


def _public_host(env: Env, hosting_mode: str, base_domain: str) -> str:
    explicit = env.str("HOSTING_PUBLIC_HOST")
    if explicit:
        return explicit
    if hosting_mode == "subdomain" and base_domain:
        return base_domain
    return _outbound_address()


def _contact_email(env: Env, reply_to: str, hosting_mode: str, base_domain: str) -> str:
    explicit = env.str("HOSTING_CONTACT_EMAIL")
    if explicit:
        address = parseaddr(explicit)[1]
        if "@" not in address or any(c in address for c in "<>\"' \n\r"):
            raise ConfigError("HOSTING_CONTACT_EMAIL must be an email address.")
        return address
    address = parseaddr(reply_to)[1]
    if "@" in address:
        return address
    if hosting_mode == "subdomain" and base_domain:
        return f"contact@{base_domain}"
    return CONTACT_EMAIL_PLACEHOLDER


def _http_url(name: str, value: str) -> str:
    if value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
            raise ConfigError(f"{name} must be an absolute http(s) URL without credentials.")
    return value


def _custom_domain_target(value: str) -> str:
    target = value.strip().lower().rstrip(".")
    if not target:
        return ""
    try:
        target = target.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ConfigError("HOSTING_CUSTOM_DOMAIN_TARGET must be a DNS hostname.") from error
    labels = target.split(".")
    if len(target) > 253 or len(labels) < 2 or labels[-1].isdigit() or any(
        not _LABEL.fullmatch(label) for label in labels
    ):
        raise ConfigError("HOSTING_CUSTOM_DOMAIN_TARGET must be a DNS hostname.")
    return target


def _custom_domain_ips(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        try:
            ipaddress.ip_address(value)
        except ValueError as error:
            raise ConfigError(f"HOSTING_CUSTOM_DOMAIN_IPS contains an invalid address: {value!r}.") from error
    return values


def _load_secret_key(env: Env, path: str, production: bool) -> str:
    explicit = env.raw("HOSTING_SECRET_KEY")
    if explicit:
        return explicit
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with FileLock(path + ".lock", timeout=20, mode=0o600):
        try:
            with open(path, encoding="utf-8") as handle:
                key = handle.read(4097).strip()
        except FileNotFoundError:
            key = ""
        else:
            if not key or len(key) > 4096:
                raise ConfigError(f"{path} is empty or invalid. Restore the original key file.")
            _check_key_permissions(path, production)
            return key
        key = secrets.token_hex(32)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".secret_key_")
        try:
            os.write(fd, key.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return key


def _check_key_permissions(path: str, production: bool) -> None:
    if os.name != "posix":
        return
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        message = f"{path} is readable by other users (mode {mode:o}); run: chmod 600 {path}"
        if production:
            raise ConfigError(message)
        log.warning(message)


def reserved_subdomains(cfg: HostingConfig, suffix: str) -> frozenset[str]:
    """Labels no wiki may use: platform records, the portal's own label and the suffix."""
    reserved = set(BASE_RESERVED_SUBDOMAINS)
    base = cfg.base_domain
    portal = cfg.effective_portal_domain
    if base and portal and portal != base and portal.endswith("." + base):
        prefix = portal[: -len(base) - 1]
        if prefix and "." not in prefix:
            reserved.add(prefix)
    if suffix:
        reserved.add(suffix)
    return frozenset(reserved)


def valid_suffix(value: str) -> bool:
    return bool(_SUFFIX.match(value)) and len(value) <= 30


# ── Loader ────────────────────────────────────────────────────────────────────


def load_config(environ: dict[str, str] | None = None, **overrides: object) -> HostingConfig:
    """Build the configuration from the environment (plus test overrides)."""
    env = Env(environ)
    run_env = env.choice(
        "HOSTING_ENV", env.str("BW_ENV", "production") or "production",
        frozenset({"production", "development", "dev", "test", "testing"}),
    )
    run_env = {"dev": "development", "testing": "test"}.get(run_env, run_env)
    production = run_env == "production"

    database_path = env.path("HOSTING_DATABASE_PATH", str(LEGACY_DATA_DIR / "hosting.db"))
    data_dir = os.path.dirname(database_path)
    instances_dir = env.path("INSTANCES_DIR", str(LEGACY_DATA_DIR / "instances"))
    platform_state_dir = env.path("HOSTING_PLATFORM_STATE_DIR", os.path.join(data_dir, "platform_state"))
    state_real, instances_real = os.path.realpath(platform_state_dir), os.path.realpath(instances_dir)
    if state_real == instances_real or state_real.startswith(instances_real + os.sep):
        raise ConfigError("HOSTING_PLATFORM_STATE_DIR must not be inside INSTANCES_DIR, where tenants can write.")

    raw_suffix = env.raw("INSTANCE_URL_SUFFIX")
    base_domain, portal_domain, suffix = normalize_domain_layout(
        env.str("BASE_DOMAIN"), env.str("PORTAL_DOMAIN"), raw_suffix or "",
        auto_flatten=_lax_bool(env, "HOSTING_AUTO_FLATTEN_DOMAIN_LAYOUT", "1"),
    )
    if raw_suffix is None and not suffix:
        suffix = "hosting"
    if suffix and not valid_suffix(suffix):
        raise ConfigError("INSTANCE_URL_SUFFIX may only contain lowercase letters, digits and hyphens.")
    hosting_mode = _detect_hosting_mode(env, base_domain)

    runtime = env.str("HOSTING_INSTANCE_RUNTIME", "docker").lower()
    if runtime not in {"process", "docker"}:
        raise ConfigError("HOSTING_INSTANCE_RUNTIME must be 'process' or 'docker'.")
    network = env.str("HOSTING_TENANT_NETWORK", "isolated" if hosting_mode == "subdomain" else "outbound").lower()
    if network not in {"isolated", "outbound"}:
        raise ConfigError("HOSTING_TENANT_NETWORK must be 'isolated' or 'outbound'.")
    if runtime == "docker" and hosting_mode in ("port", "onion") and network == "isolated":
        raise ConfigError(
            "Isolated tenants require subdomain hosting. Configure a domain and its proxy, "
            "or choose HOSTING_TENANT_NETWORK=outbound for port or onion mode."
        )

    reply_to = env.str("HOSTING_EMAIL_REPLY_TO")
    email = EmailSettings(
        provider=env.str("HOSTING_EMAIL_PROVIDER").lower(),
        api_key=env.str("HOSTING_EMAIL_API_KEY"),
        sender=env.str("HOSTING_EMAIL_FROM"),
        reply_to=reply_to,
        smtp_host=env.str("HOSTING_EMAIL_SMTP_HOST"),
        smtp_port=env.int("HOSTING_EMAIL_SMTP_PORT", 587, minimum=1, maximum=65535),
        smtp_username=env.str("HOSTING_EMAIL_SMTP_USERNAME"),
        smtp_password=env.raw("HOSTING_EMAIL_SMTP_PASSWORD") or "",
        smtp_tls=_lax_bool(env, "HOSTING_EMAIL_SMTP_TLS", "1", falsy=("0", "false", "no")),
        token_ttl_seconds=max(300, env.int("HOSTING_EMAIL_TOKEN_TTL_SECONDS", 86400)),
        daily_limit=env.int("HOSTING_EMAIL_DAILY_LIMIT", 300, minimum=0),
    )
    import_max = env.int("HOSTING_IMPORT_MAX_BYTES", 10 * 1024 ** 3, minimum=1)
    archives = ArchiveSettings(
        import_chunk_bytes=env.int("HOSTING_IMPORT_CHUNK_BYTES", 8 * 1024 * 1024, minimum=64 * 1024),
        import_max_bytes=import_max,
        import_max_extracted_bytes=env.int("HOSTING_IMPORT_MAX_EXTRACTED_BYTES", import_max * 3, minimum=1),
        import_max_members=env.int("HOSTING_IMPORT_MAX_MEMBERS", 250_000, minimum=1),
        import_min_free_bytes=env.int("HOSTING_IMPORT_MIN_FREE_BYTES", 512 * 1024 * 1024, minimum=0),
        import_temp_dir=env.path("HOSTING_IMPORT_TEMP_DIR", str(LEGACY_DATA_DIR / "tmp_imports")),
        export_temp_dir=env.path("HOSTING_EXPORT_TEMP_DIR", str(LEGACY_DATA_DIR / "tmp_exports")),
        export_compress_level=env.int("HOSTING_EXPORT_COMPRESS_LEVEL", 1, minimum=0, maximum=9),
        export_store_file_bytes=env.int("HOSTING_EXPORT_STORE_FILE_BYTES", 64 * 1024 * 1024, minimum=0),
        export_store_extensions=tuple(
            item.lower() for item in env.list("HOSTING_EXPORT_STORE_EXTENSIONS", _DEFAULT_STORE_EXTENSIONS)
        ),
    )
    limits = InstanceLimits(
        max_per_account=env.int("MAX_INSTANCES_PER_ACCOUNT", 5, minimum=0),
        duration_days=env.int("INSTANCE_DURATION_DAYS", 14, minimum=1),
        storage_limit_mb=env.int("INSTANCE_STORAGE_LIMIT_MB", 500, minimum=1),
        max_upload_mb=env.int("INSTANCE_MAX_UPLOAD_MB", 16, minimum=1),
        max_attachment_mb=env.int("INSTANCE_MAX_ATTACHMENT_MB", 100, minimum=1),
        memory_limit_mb=env.int("INSTANCE_MEMORY_LIMIT_MB", 768, minimum=128),
        nofile_limit=env.int("INSTANCE_NOFILE_LIMIT", 1024, minimum=128),
        cpu_limit=env.str("INSTANCE_CPU_LIMIT", "1.0") or "1.0",
        pids_limit=env.int("INSTANCE_PIDS_LIMIT", 256, minimum=32),
        startup_timeout_seconds=max(30, min(600, env.int("INSTANCE_STARTUP_TIMEOUT_SECONDS", 120))),
    )
    default_signup = env.str("HOSTING_DEFAULT_SIGNUP_MODE", "open").lower()
    secret_key_path = env.path("HOSTING_SECRET_KEY_PATH", str(LEGACY_DATA_DIR / ".secret_key"))
    secret_key = str(overrides.pop("secret_key", "") or "") or _load_secret_key(env, secret_key_path, production)

    values: dict[str, object] = dict(
        env=run_env,
        host=env.str("HOSTING_HOST", "127.0.0.1"),
        port=env.int("HOSTING_PORT", 5099, minimum=1, maximum=65535),
        proxy_mode=_lax_bool(env, "HOSTING_PROXY_MODE", "0"),
        preferred_url_scheme=env.str("HOSTING_PREFERRED_URL_SCHEME"),
        secret_key=secret_key,
        bootstrap_token=env.str("HOSTING_BOOTSTRAP_TOKEN"),
        password_hash_method=env.str("BW_PASSWORD_HASH_METHOD", env.str("HASH_METHOD", "auto")),
        database_path=database_path,
        backup_key_path=env.path("HOSTING_BACKUP_KEY_PATH", os.path.join(data_dir, ".backup_encryption_key")),
        instances_dir=instances_dir,
        platform_state_dir=platform_state_dir,
        base_domain=base_domain,
        portal_domain=portal_domain,
        instance_url_suffix=suffix,
        hosting_mode=hosting_mode,
        public_host=_public_host(env, hosting_mode, base_domain),
        public_scheme=env.str("HOSTING_PUBLIC_SCHEME", "https") or "https",
        port_range=(env.int("INSTANCE_PORT_START", 6001, minimum=1, maximum=65535),
                    env.int("INSTANCE_PORT_END", 7000, minimum=1, maximum=65535)),
        limits=limits,
        instance_runtime=runtime,
        tenant_network=network,
        tenant_plugin_denylist=env.list("HOSTING_TENANT_PLUGIN_DENYLIST"),
        container_image=env.str("HOSTING_CONTAINER_IMAGE", "bananawiki-tenant:latest"),
        container_internal_port=env.int("HOSTING_CONTAINER_INTERNAL_PORT", 5001, minimum=1, maximum=65535),
        subdomain_min_length=env.int("SUBDOMAIN_MIN_LENGTH", 3, minimum=1, maximum=63),
        subdomain_max_length=env.int("SUBDOMAIN_MAX_LENGTH", 40, minimum=1, maximum=63),
        custom_domain_target=_custom_domain_target(env.str("HOSTING_CUSTOM_DOMAIN_TARGET")),
        custom_domain_ips=_custom_domain_ips(env.list("HOSTING_CUSTOM_DOMAIN_IPS")),
        federation_instances=frozenset(env.list("HOSTING_FEDERATION_INSTANCES")),
        email=email,
        contact_email=_contact_email(env, reply_to, hosting_mode, base_domain),
        status_url=_http_url("HOSTING_STATUS_URL", env.str("HOSTING_STATUS_URL")),
        source_url=_http_url("BW_SOURCE_URL", env.str("BW_SOURCE_URL", DEFAULT_SOURCE_URL)) or DEFAULT_SOURCE_URL,
        archives=archives,
        default_signup_mode=default_signup if default_signup in ("open", "invite", "approval", "closed") else "open",
        maintenance_file=env.str("BANANA_MAINTENANCE_FILE", "", "BW_MAINTENANCE_FILE"),
        log_level=env.choice("HOSTING_LOG_LEVEL", "info", frozenset({"debug", "info", "warning", "error"})),
        db_busy_timeout_ms=env.int("HOSTING_DB_BUSY_TIMEOUT_MS", 5000, minimum=100, maximum=30000),
        testing=run_env == "test",
    )
    values.update(overrides)
    cfg = HostingConfig(**values)  # type: ignore[arg-type]
    if cfg.port_range[0] >= cfg.port_range[1]:
        raise ConfigError("INSTANCE_PORT_START must be lower than INSTANCE_PORT_END.")
    return cfg
