"""Hosting portal configuration, read once from the environment.

Every variable BananaWiki Hosting 1.4 honoured is still honoured with the same
meaning and default, so an existing ``config/app.env`` keeps working after the
upgrade. Paths default to ``<repository>/hosting/data`` like 1.4; managed
installations set them explicitly to the writable ``data/`` directory.

When ``HOSTING_PUBLIC_HOST`` is unset in port mode the portal uses the
outbound interface address (found without sending traffic), never a
third-party IP-echo service. Nothing changes the configuration at run
time; the instance URL suffix chosen in the platform settings is applied
by :mod:`bananawiki.hosting.urls`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import socket
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from ..core.env import ConfigError, Env
from ..core.i18n import FALLBACK

log = logging.getLogger("bananawiki.hosting.config")

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
LEGACY_DATA_DIR = REPO_ROOT / "hosting" / "data"
DEFAULT_SOURCE_URL = "https://github.com/BananaSuite/BananaWiki"
CONTACT_EMAIL_PLACEHOLDER = "contact@localhost"
# The name the platform goes by in its pages, emails and authenticator apps (HOSTING_SERVICE_NAME).
DEFAULT_SERVICE_NAME = "BananaWiki Hosting"
SERVICE_NAME_MAX_LENGTH = 60
# The pages /terms and /privacy lead to (HOSTING_TERMS_URL, HOSTING_PRIVACY_URL; /compliance is the terms).
LEGAL_PAGES = ("terms", "privacy")
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
    # Messages per day sent at one account's request (verification links, wiki notices); 0 = no cap.
    account_daily_limit: int = 10


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
    # Accounts younger than ``new_account_days`` may hold ``new_account_max_instances`` wikis, and all
    # their wikis together at most ``new_account_share_percent`` of the platform's wiki limit.
    new_account_days: int = 7
    new_account_max_instances: int = 1
    new_account_share_percent: int = 50
    # Days before expiry on which the owner is warned by email (largest first); empty = no warnings.
    expiry_warning_days: tuple[int, ...] = (3, 1)


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
    tts_gpu_tenant_tokens: bool
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
    log_format: str
    db_busy_timeout_ms: int = 5000
    min_form_seconds: float = 1.5
    # Accounts the sign-up form creates per client address per day; 0 = no cap.
    signups_per_ip_per_day: int = 10
    # Days the moderation log (hosting_events) keeps an entry; 0 = forever. The privacy policy
    # promises deletion at the latest 10 years after the decision.
    event_retention_days: int = 3650
    # Wikis the maintenance service restarts at once after an update or a backup; the updater
    # sizes its readiness timeout from the same variable.
    recovery_max_workers: int = 2
    # Version (YYYY-MM-DD) of the terms of service and privacy policy the operator publishes, as
    # printed on them (HOSTING_TERMS_VERSION; default accounts.TERMS_VERSION): what sign-ups and
    # /account/terms record as accepted.
    terms_version: str = ""
    # Terms version every account must have accepted; older ones are asked to accept the current
    # terms before anything else. "" = nobody is asked (new sign-ups always accept them). Never later
    # than terms_version (load_config refuses that).
    terms_required_version: str = ""
    # The name the platform goes by: page titles and top bar, emails, the authenticator label.
    service_name: str = DEFAULT_SERVICE_NAME
    # The operator's own legal pages as (page, language, https URL); language "" is the page for every
    # interface language without its own. Empty: /terms and /privacy lead to the static site on the base
    # domain (see routes.public). Read through legal_url().
    legal_urls: tuple[tuple[str, str, str], ...] = ()
    testing: bool = False
    # Accept custom domains whose records are proxied by Cloudflare (only Cloudflare addresses visible).
    custom_domain_allow_proxied: bool = True
    # Third-party code is an operator opt-in; built-in wiki features remain available.
    allow_tenant_plugins: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def effective_portal_domain(self) -> str:
        return self.portal_domain or self.base_domain

    @property
    def tenant_network_outbound(self) -> bool:
        return self.instance_runtime == "docker" and self.tenant_network == "outbound"

    def legal_url(self, page: str, language: str) -> str:
        """The operator's address of *page* (``terms`` or ``privacy``) for *language*, or "" for none."""
        urls = {(name, lang): url for name, lang, url in self.legal_urls}
        return urls.get((page, language)) or urls.get((page, ""), "")


# Helpers


def _lax_bool(env: Env, name: str, default: str, falsy: tuple[str, ...] = ("0", "false", "no", "off", "")) -> bool:
    """1.4 read these flags as "anything but a false word is on"; keep that."""
    return env.str(name, default).lower() not in falsy


def _warning_days(env: Env) -> tuple[int, ...]:
    days = set()
    for item in env.list("HOSTING_EXPIRY_WARNING_DAYS", "3,1"):
        try:
            value = int(item)
        except ValueError:
            raise ConfigError("HOSTING_EXPIRY_WARNING_DAYS must list whole numbers of days, such as 3,1.") from None
        if not 0 <= value <= 365:
            raise ConfigError("HOSTING_EXPIRY_WARNING_DAYS values must be between 1 and 365 (0 for none).")
        if value:
            days.add(value)
    return tuple(sorted(days, reverse=True))


def _terms_version(env: Env, name: str, default: str = "") -> str:
    """A terms version: a real ``YYYY-MM-DD`` date, so that versions order as text (see accounts.terms_outdated)."""
    value = env.str(name) or default
    if value:
        try:
            valid = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and date.fromisoformat(value) is not None
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError(f"{name} must be the terms' version date, such as 2026-01-31.")
    return value


def _check_terms(cfg: HostingConfig) -> None:
    """Accounts can only accept the published version: a later required one would lock everyone out."""
    if cfg.terms_required_version and cfg.terms_required_version > cfg.terms_version:
        raise ConfigError(
            f"HOSTING_TERMS_REQUIRED_VERSION ({cfg.terms_required_version}) is later than the terms accounts "
            f"accept ({cfg.terms_version}). Set HOSTING_TERMS_VERSION to the version printed on the terms "
            "you publish, then require that version or an earlier one."
        )


def _recovery_workers(env: Env) -> int:
    """``BW_HOSTING_RECOVERY_MAX_WORKERS``, read exactly as the updater reads it.

    The updater (``bananawiki.ops.system``, readiness timeout) takes it from
    ``config/app.env`` clamped to 1-16, and 2 for anything that is not a
    whole number. The portal never read it before 1.7, so a value the updater
    tolerated must not stop the portal from starting: it is clamped the same
    way, with a warning.
    """
    raw = (env.raw("BW_HOSTING_RECOVERY_MAX_WORKERS") or "").strip() or "2"
    try:
        value = int(raw)
    except ValueError:
        log.warning("BW_HOSTING_RECOVERY_MAX_WORKERS=%r is not a whole number; using 2.", raw)
        return 2
    if not 1 <= value <= 16:
        log.warning("BW_HOSTING_RECOVERY_MAX_WORKERS=%d is outside 1-16; using %d.", value,
                    max(1, min(16, value)))
    return max(1, min(16, value))


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


def _https_url(name: str, value: str) -> str:
    """An absolute ``https://`` address for visitors' browsers: no credentials, spaces or control characters.

    An onion address may use ``http://``: Tor already encrypts the connection.
    """
    if value:
        try:
            parts = urlsplit(value)
            onion = parts.scheme == "http" and (parts.hostname or "").endswith(".onion")
            # The port is parsed lazily: an invalid one would otherwise fail every redirect to the page.
            valid = (parts.scheme == "https" or onion) and bool(parts.hostname) and "@" not in parts.netloc \
                and (parts.port is None or parts.port > 0)
        except ValueError:  # such as an unclosed [IPv6 address or a port that is not a number up to 65535
            valid = False
        if not valid or any(char.isspace() or not char.isprintable() for char in value):
            raise ConfigError(f"{name} must be an absolute https:// URL (http:// for an onion address) "
                              "without credentials or spaces.")
    return value


def _legal_urls(env: Env, languages: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """``HOSTING_TERMS_URL``, ``HOSTING_PRIVACY_URL`` and their ``_<LANG>`` variants for other interface languages.

    Both pages or neither: one on the operator's site and the other on a static site they may not run
    would send visitors to a missing page. A language variant only replaces its page for that language.
    """
    urls: list[tuple[str, str, str]] = []
    for page in LEGAL_PAGES:
        name = f"HOSTING_{page.upper()}_URL"
        default = _https_url(name, env.str(name))
        if default:
            urls.append((page, "", default))
        for language in languages:
            variant = f"{name}_{language.upper()}"
            value = _https_url(variant, env.str(variant))
            if value and not default:
                raise ConfigError(f"{variant} needs {name}: it only replaces that address for one language.")
            if value:
                urls.append((page, language, value))
    if len({page for page, _language, _url in urls}) == 1:
        raise ConfigError("Set HOSTING_TERMS_URL and HOSTING_PRIVACY_URL together (they may be the same address), "
                          "or neither for the pages of the static site on BASE_DOMAIN.")
    return tuple(urls)


def _service_name(env: Env) -> str:
    """``HOSTING_SERVICE_NAME``: one short line, shown escaped; braces would read as translation placeholders."""
    value = env.str("HOSTING_SERVICE_NAME") or DEFAULT_SERVICE_NAME
    if (len(value) > SERVICE_NAME_MAX_LENGTH or "{" in value or "}" in value
            or any(unicodedata.category(char)[0] == "C" or unicodedata.category(char) in ("Zl", "Zp")
                   for char in value)):
        raise ConfigError(f"HOSTING_SERVICE_NAME must be at most {SERVICE_NAME_MAX_LENGTH} characters on one line, "
                          "without braces or control characters.")
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


# Loader


def load_config(environ: dict[str, str] | None = None, **overrides: object) -> HostingConfig:
    """Build the configuration from the environment (plus test overrides)."""
    from .accounts import TERMS_VERSION  # the default: the version this release ships with
    from .i18n import LANGUAGES

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
        account_daily_limit=env.int("HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT", 10, minimum=0),
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
        duration_days=env.int("INSTANCE_DURATION_DAYS", 14, minimum=0),
        storage_limit_mb=env.int("INSTANCE_STORAGE_LIMIT_MB", 500, minimum=1),
        max_upload_mb=env.int("INSTANCE_MAX_UPLOAD_MB", 16, minimum=1),
        max_attachment_mb=env.int("INSTANCE_MAX_ATTACHMENT_MB", 100, minimum=1),
        memory_limit_mb=env.int("INSTANCE_MEMORY_LIMIT_MB", 768, minimum=128),
        nofile_limit=env.int("INSTANCE_NOFILE_LIMIT", 1024, minimum=128),
        cpu_limit=env.str("INSTANCE_CPU_LIMIT", "1.0") or "1.0",
        pids_limit=env.int("INSTANCE_PIDS_LIMIT", 256, minimum=32),
        startup_timeout_seconds=max(30, min(600, env.int("INSTANCE_STARTUP_TIMEOUT_SECONDS", 120))),
        new_account_days=env.int("HOSTING_NEW_ACCOUNT_DAYS", 7, minimum=0, maximum=365),
        new_account_max_instances=env.int("HOSTING_NEW_ACCOUNT_MAX_INSTANCES", 1, minimum=0),
        new_account_share_percent=env.int("HOSTING_NEW_ACCOUNT_SHARE_PERCENT", 50, minimum=0, maximum=100),
        expiry_warning_days=_warning_days(env),
    )
    legal_urls = _legal_urls(env, tuple(language for language in LANGUAGES if language != FALLBACK))
    if legal_urls and not env.str("HOSTING_TERMS_VERSION"):
        log.warning("HOSTING_TERMS_URL is set but HOSTING_TERMS_VERSION is not: accounts that accept your terms are "
                    "recorded as accepting version %s, this release's default. Set HOSTING_TERMS_VERSION to the "
                    "version date printed on your terms.", TERMS_VERSION)
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
        allow_tenant_plugins=env.bool("HOSTING_ALLOW_TENANT_PLUGINS", False),
        tts_gpu_tenant_tokens=env.bool("HOSTING_TTS_GPU_TENANT_TOKENS", True),
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
        service_name=_service_name(env),
        legal_urls=legal_urls,
        archives=archives,
        default_signup_mode=default_signup if default_signup in ("open", "invite", "approval", "closed") else "open",
        maintenance_file=env.str("BANANA_MAINTENANCE_FILE", "", "BW_MAINTENANCE_FILE"),
        log_level=env.choice("HOSTING_LOG_LEVEL", "info", frozenset({"debug", "info", "warning", "error"})),
        log_format=env.choice("HOSTING_LOG_FORMAT", "text", frozenset({"text", "json"})),
        db_busy_timeout_ms=env.int("HOSTING_DB_BUSY_TIMEOUT_MS", 5000, minimum=100, maximum=30000),
        signups_per_ip_per_day=env.int("HOSTING_SIGNUPS_PER_IP_PER_DAY", 10, minimum=0),
        event_retention_days=env.int("HOSTING_EVENT_RETENTION_DAYS", 3650, minimum=0, maximum=36500),
        recovery_max_workers=_recovery_workers(env),
        terms_version=_terms_version(env, "HOSTING_TERMS_VERSION", TERMS_VERSION),
        terms_required_version=_terms_version(env, "HOSTING_TERMS_REQUIRED_VERSION"),
        testing=run_env == "test",
        custom_domain_allow_proxied=env.bool("HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED", True),
    )
    values.update(overrides)
    cfg = HostingConfig(**values)  # type: ignore[arg-type]
    _check_terms(cfg)
    if cfg.port_range[0] >= cfg.port_range[1]:
        raise ConfigError("INSTANCE_PORT_START must be lower than INSTANCE_PORT_END.")
    if cfg.allow_tenant_plugins and cfg.hosting_mode in ("port", "onion"):
        # Cookies ignore the port: every wiki would receive, and could set, the portal's session cookie.
        raise ConfigError(
            "HOSTING_ALLOW_TENANT_PLUGINS=1 requires subdomain hosting: in port and onion mode the portal "
            "and every wiki share one host, so a wiki's plugins could read or replace the portal's session "
            "cookie. Configure a domain and its proxy, or set HOSTING_ALLOW_TENANT_PLUGINS=0."
        )
    return cfg
