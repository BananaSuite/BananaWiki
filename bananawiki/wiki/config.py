"""Wiki configuration, read once from the environment.

Every ``BW_*`` variable that BananaWiki 1.4 honoured is still honoured with the
same meaning, so an existing systemd unit, container environment or ``.env``
file keeps working after the upgrade. See ``docs/configuration.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from ..core.env import ConfigError, Env
from ..core.mail import MailSettings, settings_from_env

log = logging.getLogger("bananawiki.config")

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent

KIB = 1024
MIB = 1024 * KIB

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_DOCUMENT_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "txt", "md", "csv", "json", "xml", "zip", "tar", "gz",
})
AUDIO_EXTENSIONS = frozenset({"mp3", "ogg", "wav", "m4a"})
VIDEO_EXTENSIONS = frozenset({"mp4", "webm"})
ATTACHMENT_EXTENSIONS = _DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
CHAT_EXTENSIONS = _DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS

DEFAULT_SOURCE_URL = "https://github.com/BananaSuite/BananaWiki"


@dataclass(frozen=True)
class Folders:
    """Where persistent files live. All default to directories under the instance dir."""

    uploads: str
    favicons: str
    attachments: str
    chat_attachments: str
    kanban_attachments: str
    custom_page_files: str
    tts: str
    piper_voices: str
    exports: str
    plugins: str
    logs: str


@dataclass(frozen=True)
class Config:
    env: str
    instance_dir: str
    database_path: str
    secret_key: str = field(repr=False)
    setup_token: str = field(repr=False)
    folders: Folders
    host: str
    port: int
    proxy_mode: bool
    preferred_url_scheme: str
    session_cookie_name: str
    secure_cookies: bool | None
    password_hash_method: str
    source_url: str
    log_level: str
    log_file: str
    default_language: str
    max_content_length: int
    max_attachment_size: int
    max_kanban_attachment_size: int
    max_custom_page_file_size: int
    max_custom_page_video_size: int
    max_import_size: int
    background_image_max_upload: int
    background_image_max_pixels: int
    background_image_max_dimension: int
    remember_me_days: int
    session_days: int
    min_form_seconds: float
    memory_limit_mb: int
    nofile_limit: int
    integrity_check_interval: int
    # Platform / managed hosting
    managed_hosting: bool
    easy_wiki: bool
    easy_deployment: bool
    instance_expires_at: str | None
    platform_instance_id: str
    storage_limit_bytes: int
    forbid_public_mode: bool
    forbid_page_builder: bool
    forbid_public_builder_pages: bool
    managed_tts_disabled: bool
    platform_upload_blacklist: tuple[str, ...]
    managed_plugin_denylist: frozenset[str]
    allow_external_plugins: bool
    federation_enabled: bool
    maintenance_file: str
    # Whole-site import replaces the database; hosts must opt in (BW_ALLOW_SITE_IMPORT).
    allow_site_import: bool
    # Platform OAuth (sign in with the hosting portal)
    platform_oauth: dict[str, str] = field(default_factory=dict, repr=False)
    # Database tuning
    db_busy_timeout_ms: int = 5000
    db_cache_kib: int = 4096
    db_synchronous: str = "FULL"
    # Background jobs
    run_background_jobs: bool = True
    testing: bool = False
    # Outgoing email set by the operator (BW_MAIL_* / BW_SMTP_*); wins over the admin settings.
    mail: MailSettings | None = field(default=None, repr=False)
    # Public address of the wiki for links in emails (BW_BASE_URL).
    base_url: str = ""

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def remember_me_seconds(self) -> int:
        return self.remember_me_days * 86400


def default_instance_dir() -> Path:
    """``instance/`` beside a source checkout, else ``./instance`` for installed packages.

    An installed package lives in site-packages, which must never hold data.
    """
    if (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "bananawiki").is_dir():
        return REPO_ROOT / "instance"
    return Path.cwd() / "instance"


def _load_secret_key(instance_dir: str, env: Env, production: bool) -> str:
    explicit = env.str("SECRET_KEY", "", "BW_SECRET_KEY")
    if explicit:
        if len(explicit) < 32 and production:
            raise ConfigError("SECRET_KEY must be at least 32 characters long.")
        return explicit
    os.makedirs(instance_dir, mode=0o700, exist_ok=True)
    key_file = os.path.join(instance_dir, ".secret_key")
    with FileLock(key_file + ".lock", timeout=20, mode=0o600):
        try:
            with open(key_file, encoding="utf-8") as handle:
                key = handle.read(4097).strip()
        except FileNotFoundError:
            key = ""
        else:
            if not key or len(key) > 4096:
                raise ConfigError(f"{key_file} is empty or invalid. Restore the original key file.")
            _check_key_permissions(key_file, production)
            return key
        key = secrets.token_hex(32)
        fd, tmp = tempfile.mkstemp(dir=instance_dir, prefix=".secret_key_")
        try:
            os.write(fd, key.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, key_file)
        return key


def _check_key_permissions(key_file: str, production: bool) -> None:
    if os.name != "posix":
        return
    mode = os.stat(key_file).st_mode & 0o777
    if mode & 0o077:
        message = f"{key_file} is readable by other users (mode {mode:o}); run: chmod 600 {key_file}"
        if production:
            raise ConfigError(message)
        log.warning(message)


def _validate_source_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ConfigError("BW_SOURCE_URL must be an absolute http(s) URL without credentials.")
    return url


def _validate_base_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise ConfigError("BW_BASE_URL must be an absolute http(s) URL such as https://wiki.example.org.")
    return url.rstrip("/")


def load_config(environ: dict[str, str] | None = None, **overrides: object) -> Config:
    """Build the configuration from the environment (plus test overrides)."""
    env = Env(environ)
    run_env = env.choice(
        "BW_ENV", "production", frozenset({"production", "development", "dev", "test", "testing"})
    )
    run_env = {"dev": "development", "testing": "test"}.get(run_env, run_env)
    production = run_env == "production"

    instance_dir = env.path("BW_INSTANCE_DIR", str(default_instance_dir()))

    def data(name: str, var: str) -> str:
        return env.path(var, os.path.join(instance_dir, name))

    folders = Folders(
        uploads=data("uploads", "BW_UPLOAD_FOLDER"),
        favicons=data("favicons", "BW_FAVICON_UPLOAD_FOLDER"),
        attachments=data("attachments", "BW_ATTACHMENT_FOLDER"),
        chat_attachments=data("chat_attachments", "BW_CHAT_ATTACHMENT_FOLDER"),
        kanban_attachments=data("kanban_attachments", "BW_KANBAN_ATTACHMENT_FOLDER"),
        custom_page_files=data("custom_page_files", "BW_CUSTOM_PAGE_FILES_FOLDER"),
        tts=data("tts", "BW_TTS_FOLDER"),
        piper_voices=data("piper-voices", "BW_TTS_PIPER_VOICE_DIR"),
        exports=data("tmp_exports", "BW_SITE_EXPORT_TEMP_DIR"),
        plugins=env.path("BW_EXTERNAL_PLUGINS_DIR", os.path.join(instance_dir, "plugins")),
        logs=os.path.dirname(data(os.path.join("logs", "bananawiki.log"), "BW_LOG_FILE")),
    )

    secret_key = str(overrides.pop("secret_key", "") or "") or _load_secret_key(instance_dir, env, production)
    setup_token = env.str("BW_SETUP_TOKEN") or hmac.new(
        secret_key.encode("utf-8"), b"initial-admin-setup", hashlib.sha256
    ).hexdigest()

    managed = env.bool("BW_MANAGED_HOSTING", False, "BW_HOSTED_MODE")
    secure_raw = env.raw("BW_SECURE_COOKIES")
    oauth_keys = (
        "CLIENT_ID", "CLIENT_SECRET", "AUTHORIZE_URL", "TOKEN_URL", "USERINFO_URL", "PORTAL_BASE",
        "LINK_URL", "LINK_STATUS_URL", "UNLINK_URL", "VERIFY_URL",
    )
    platform_oauth: dict[str, str] = {}
    if env.bool("BW_PLATFORM_OAUTH_ENABLED", False):
        platform_oauth = {key.lower(): env.str(f"BW_PLATFORM_OAUTH_{key}") for key in oauth_keys}

    default_max_body = 16 * MIB
    values: dict[str, object] = dict(
        env=run_env,
        instance_dir=instance_dir,
        database_path=env.path("BW_DATABASE_PATH", os.path.join(instance_dir, "bananawiki.db")),
        secret_key=secret_key,
        setup_token=setup_token,
        folders=folders,
        host=env.str("BW_HOST", "127.0.0.1"),
        port=env.int("BW_PORT", 5001, minimum=1, maximum=65535),
        proxy_mode=env.bool("BW_PROXY_MODE", False),
        preferred_url_scheme=env.str("BW_PREFERRED_URL_SCHEME", ""),
        session_cookie_name=env.str("BW_SESSION_COOKIE_NAME", "bw_session"),
        secure_cookies=None if secure_raw is None else env.bool("BW_SECURE_COOKIES"),
        password_hash_method=env.str("BW_PASSWORD_HASH_METHOD", "auto", "HASH_METHOD"),
        source_url=_validate_source_url(env.str("BW_SOURCE_URL", DEFAULT_SOURCE_URL)),
        log_level=env.choice(
            "BW_LOGGING_LEVEL", "medium", frozenset({"off", "minimal", "medium", "verbose", "debug"})
        ),
        log_file=env.path("BW_LOG_FILE", os.path.join(instance_dir, "logs", "bananawiki.log")),
        default_language=env.str("BW_DEFAULT_INTERFACE_LANGUAGE", "en").lower() or "en",
        max_content_length=env.int("BW_MAX_CONTENT_LENGTH_BYTES", default_max_body, minimum=MIB),
        max_attachment_size=env.int("BW_MAX_ATTACHMENT_SIZE_BYTES", 100 * MIB, minimum=KIB),
        max_kanban_attachment_size=100 * MIB,
        max_custom_page_file_size=16 * MIB,
        max_custom_page_video_size=100 * MIB,
        max_import_size=500 * MIB,
        background_image_max_upload=env.int("BW_BACKGROUND_IMAGE_MAX_UPLOAD_SIZE_BYTES", 4 * MIB, minimum=KIB),
        background_image_max_pixels=env.int("BW_BACKGROUND_IMAGE_MAX_PIXELS", 16_000_000, minimum=1),
        background_image_max_dimension=env.int("BW_BACKGROUND_IMAGE_MAX_DIMENSION", 2560, minimum=16),
        remember_me_days=30,
        session_days=7,
        min_form_seconds=env.float("BW_MIN_FORM_SECONDS", 0.4, minimum=0.0),
        memory_limit_mb=env.int("BW_MEMORY_LIMIT_MB", 0, minimum=0),
        nofile_limit=env.int("BW_NOFILE_LIMIT", 0, minimum=0),
        integrity_check_interval=env.int("BW_DB_INTEGRITY_CHECK_INTERVAL_SECONDS", 3600, minimum=0),
        managed_hosting=managed,
        easy_wiki=env.bool("BW_EASY_WIKI", False),
        easy_deployment=env.bool("BW_EASY_DEPLOYMENT", False),
        instance_expires_at=env.str("BW_INSTANCE_EXPIRES_AT") or None,
        platform_instance_id=env.str("BW_PLATFORM_INSTANCE_ID"),
        storage_limit_bytes=env.int("BW_STORAGE_LIMIT_BYTES", 0, minimum=0),
        forbid_public_mode=env.bool("BW_FORBID_PUBLIC_MODE", False),
        forbid_page_builder=env.bool("BW_FORBID_PAGE_BUILDER", False),
        forbid_public_builder_pages=env.bool("BW_FORBID_PUBLIC_BUILDER_PAGES", managed),
        managed_tts_disabled=env.bool("BW_MANAGED_TTS_DISABLED", False),
        platform_upload_blacklist=tuple(
            item.lower().lstrip(".") for item in env.list("BW_PLATFORM_UPLOAD_BLACKLIST")
        ),
        managed_plugin_denylist=frozenset(env.list("BW_MANAGED_PLUGIN_DENYLIST")),
        allow_external_plugins=env.bool("BW_ALLOW_EXTERNAL_PLUGINS", True) and (
            not managed or env.str("BW_PLUGIN_ISOLATION").lower() == "container"
        ),
        federation_enabled=env.bool("BW_FEDERATION_ENABLED", False),
        maintenance_file=env.str("BW_MAINTENANCE_FILE", "", "BANANA_MAINTENANCE_FILE"),
        allow_site_import=env.bool("BW_ALLOW_SITE_IMPORT", not managed),
        platform_oauth=platform_oauth,
        db_busy_timeout_ms=env.int("BW_DB_BUSY_TIMEOUT_MS", 5000, minimum=100, maximum=30000),
        db_cache_kib=env.int("BW_DB_CACHE_KIB", 4096, minimum=256, maximum=65536),
        db_synchronous=env.choice("BW_DB_SYNCHRONOUS", "full", frozenset({"full", "normal"})).upper(),
        run_background_jobs=env.bool("BW_BACKGROUND_JOBS", True),
        testing=run_env == "test",
        mail=settings_from_env(env, "BW_"),
        base_url=_validate_base_url(env.str("BW_BASE_URL")),
    )
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]
