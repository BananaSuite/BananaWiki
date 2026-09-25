"""
BananaWiki Configuration File
Customize your instance settings here.

Most settings can be overridden via environment variables prefixed with
``BW_``: this is used by the managed hosting platform to configure
per-instance settings without modifying this file.
"""

import os
import hashlib
import hmac
import secrets
import tempfile

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Operator opt-in. Hosted tenants receive this only through the provider policy.
FEDERATION_ENABLED = os.environ.get("BW_FEDERATION_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# A wiki administrator on the public hosting service is not a host operator.
# External Python plugins are therefore enabled only for self-hosting or when
# the portal proves the tenant is running in its dedicated container boundary.
# A shared-process worker must never opt in: that would grant host code
# execution and cross-tenant filesystem access.
#
# On a self-hosted wiki they stay on unless BW_ALLOW_EXTERNAL_PLUGINS is 0.
# An external plugin runs as the wiki itself, so any admin who installs one
# gets complete control of the wiki, and of the files the service account can
# reach. The admin pages say so and ask for the admin's password again before
# an external plugin is imported, enabled or deleted. Set the variable to 0 to
# take the upload form away and ignore external plugin folders.
MANAGED_HOSTING = os.environ.get("BW_MANAGED_HOSTING", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# ISO-8601 timestamp of when the hosting platform will auto-terminate this
# instance.  Injected by the hosting ``_instance_env()`` helper.  ``None``
# means the instance is perpetual or the env var was not set.
INSTANCE_EXPIRES_AT = os.environ.get("BW_INSTANCE_EXPIRES_AT") or None

ALLOW_EXTERNAL_PLUGINS = (
    os.environ.get("BW_ALLOW_EXTERNAL_PLUGINS", "1").strip().lower()
    not in ("0", "false", "no", "off")
    and (
        not MANAGED_HOSTING
        or os.environ.get("BW_PLUGIN_ISOLATION", "").strip().lower()
        == "container"
    )
)

# Plugin ids a hosted tenant may not install or enable, even with a container.
# file_manager edited executable source and banana_ai called arbitrary outbound
# endpoints. Neither ships any more, but both circulated as .bwplugin archives.
# Under BW_MANAGED_HOSTING the wiki refuses these ids at upload, skips external
# folders using them at discovery, and refuses them at startup and at every
# enable request. Ids are compared without regard to case. The list is keyed on
# ids only, so it is a policy switch rather than a security boundary: the same
# code uploaded under another id is not caught, and while external plugins are
# allowed at all, the tenant container is what contains them. The variable can
# only add ids to the list; the hosting platform sets it for every tenant from
# its own HOSTING_TENANT_PLUGIN_DENYLIST.
MANAGED_PLUGIN_DENYLIST = frozenset(
    {"file_manager", "banana_ai"}
    | {
        item.strip()
        for item in os.environ.get("BW_MANAGED_PLUGIN_DENYLIST", "").split(",")
        if item.strip()
    }
)
FORBID_PUBLIC_MODE = os.environ.get("BW_FORBID_PUBLIC_MODE", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
FORBID_PAGE_BUILDER = os.environ.get("BW_FORBID_PAGE_BUILDER", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# Builder pages are private to authenticated wiki users unless the host has
# explicitly approved both public wiki access and the page builder.
FORBID_PUBLIC_BUILDER_PAGES = os.environ.get(
    "BW_FORBID_PUBLIC_BUILDER_PAGES", "1" if MANAGED_HOSTING else "0"
).strip().lower() in ("1", "true", "yes", "on")
# Set by the hosting platform when the admin has disabled TTS generation for
# this instance (global off, or excluded by whitelist / blacklist policy).
MANAGED_TTS_DISABLED = os.environ.get("BW_MANAGED_TTS_DISABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Hard hosting storage ceiling.  Zero means unlimited (self-hosted default).
# Managed hosting injects the effective per-instance value.  The application
# checks it before mutations while the platform supervisor stops instances
# that substantially exceed it, protecting SQLite and the host filesystem.
try:
    STORAGE_LIMIT_BYTES = max(0, int(os.environ.get("BW_STORAGE_LIMIT_BYTES", "0")))
except (TypeError, ValueError):
    STORAGE_LIMIT_BYTES = 0


def _apply_process_resource_limits():
    """Apply optional per-process ceilings for managed instances.

    These complement (but do not replace) cgroups.  They prevent a single
    Gunicorn worker or TTS worker from consuming all virtual memory or file
    descriptors on the host.  Limits are opt-in for self-hosting and injected
    by BananaWiki Hosting.
    """
    if os.name != "posix":
        return
    try:
        import resource
        memory_mb = int(os.environ.get("BW_MEMORY_LIMIT_MB", "0") or 0)
        nofile = int(os.environ.get("BW_NOFILE_LIMIT", "0") or 0)
        if memory_mb > 0:
            ceiling = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        if nofile > 0:
            _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = min(nofile, hard if hard != resource.RLIM_INFINITY else nofile)
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))
    except (ImportError, OSError, ValueError):
        # Unsupported kernels/containers should still boot; systemd limits are
        # the stronger production boundary and remain documented.
        pass


_apply_process_resource_limits()

# Networking Configuration
# Port Gunicorn binds to. Each app on the server gets its own port.
PORT = int(os.environ.get("BW_PORT", "5001"))

# Gunicorn bind address. The default (127.0.0.1) is correct for the typical
# deployment where nginx acts as the public-facing reverse proxy.
# Change to 0.0.0.0 only if Gunicorn must be reachable directly (no proxy).
HOST = os.environ.get("BW_HOST", "127.0.0.1")

# Reverse Proxy Support
# Set to True when running behind nginx (or any other reverse proxy).
# This enables Flask's ProxyFix so the app sees the real client IP and
# the correct scheme (https) from the X-Forwarded-* headers.
#
# Enable only when the app is reachable through your trusted reverse proxy.
PROXY_MODE = os.environ.get("BW_PROXY_MODE", "0").strip().lower() not in ("0", "false", "no", "")

# URL scheme to use when generating external URLs (_external=True).
# Override to "http" when deploying behind a Tor onion service.
# Defaults to "https" when PROXY_MODE is enabled (clearnet production).
PREFERRED_URL_SCHEME = os.environ.get("BW_PREFERRED_URL_SCHEME", "").strip() or ""

# Security
INSTANCE_DIR = os.environ.get("BW_INSTANCE_DIR", os.path.join(BASE_DIR, "instance"))
SECRET_KEY_FILE = os.path.join(INSTANCE_DIR, ".secret_key")


def _read_secret_key_from_env():
    """Return the secret key from the ``SECRET_KEY`` env var, or None."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    return env_key or None


def _verify_key_file_permissions():
    """Check that the secret key file is not group-/world-readable.

    In production mode (``BW_ENV`` not ``development``/``test``) we refuse
    to boot if the file has overly permissive permissions, because a leaked
    signing key lets an attacker forge session cookies for any account.  In
    development mode we only warn.  On Windows, POSIX permission bits are not
    meaningful: the check is skipped.
    """
    if os.name == "nt":
        return
    try:
        mode = os.stat(SECRET_KEY_FILE).st_mode & 0o777
        if mode & 0o077:  # any group or other access
            import logging
            msg = (
                f"Secret key file {SECRET_KEY_FILE} has overly "
                f"permissive permissions (mode {mode:o}). "
                f"Expected 0600. Run: chmod 600 {SECRET_KEY_FILE}"
            )
            env = os.environ.get("BW_ENV", "production").strip().lower()
            if env in ("development", "dev", "test", "testing"):
                logging.getLogger("bananawiki").warning(msg)
            else:
                raise SystemExit(
                    f"FATAL: {msg}\n"
                    "Set BW_ENV=development to bypass this check "
                    "during local hacking."
                )
    except OSError:
        pass  # cannot stat the file: skip the permission check


def _read_secret_key_from_file():
    """Read and return the secret key from the key file, or None if absent."""
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            file_key = f.read(4097).strip()
        if len(file_key) > 4096 or not file_key:
            raise ValueError("The application key file is empty or invalid; restore its original key")
        if file_key:
            _verify_key_file_permissions()
            return file_key
    except FileNotFoundError:
        pass
    return None


def _generate_and_write_secret_key(instance_dir):
    """Generate a new random secret key and write it atomically to the key file.

    Uses a write-to-temp-then-rename strategy so concurrent readers never see
    a partially-written file.
    """
    new_key = secrets.token_hex(32)
    fd, tmp_path = tempfile.mkstemp(dir=instance_dir, prefix=".secret_key_")
    try:
        os.write(fd, new_key.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SECRET_KEY_FILE)
    except Exception:
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
    return new_key


def _load_secret_key():
    from filelock import FileLock
    key = _read_secret_key_from_env()
    if key:
        return key
    instance_dir = os.path.dirname(SECRET_KEY_FILE)
    os.makedirs(instance_dir, mode=0o700, exist_ok=True)
    # Keep all workers on the same key during a concurrent first start.
    with FileLock(SECRET_KEY_FILE + ".lock", timeout=20, mode=0o600):
        key = _read_secret_key_from_file()
        if key:
            return key
        return _generate_and_write_secret_key(instance_dir)


SECRET_KEY = _load_secret_key()
SETUP_TOKEN = os.environ.get("BW_SETUP_TOKEN", "").strip() or hmac.new(
    SECRET_KEY.encode("utf-8"), b"initial-admin-setup", hashlib.sha256,
).hexdigest()


# Session cookie name: configurable so that hosted instances on the same
# hostname (port mode) each get their own cookie and don't collide.
SESSION_COOKIE_NAME = os.environ.get("BW_SESSION_COOKIE_NAME", "bw_session")

# Password hash method for newly created or changed passwords.
# ``auto`` keeps Linux on Werkzeug's default scrypt when available, while
# falling back to pbkdf2:sha256 on Python/OpenSSL builds that do not expose
# hashlib.scrypt (common on older macOS Python builds and some Windows setups).
# Override with BW_PASSWORD_HASH_METHOD=scrypt or pbkdf2:sha256 if needed.
PASSWORD_HASH_METHOD = os.environ.get(
    "BW_PASSWORD_HASH_METHOD",
    os.environ.get("HASH_METHOD", "auto"),
)

# Database. The default follows INSTANCE_DIR, so setting BW_INSTANCE_DIR alone
# keeps the database with the rest of the persistent data rather than inside
# the source tree. See MIGRATION.md if an older installation set that variable
# without also setting BW_DATABASE_PATH.
DATABASE_PATH = os.environ.get(
    "BW_DATABASE_PATH",
    os.path.join(INSTANCE_DIR, "bananawiki.db"),
)

# Uploads
UPLOAD_FOLDER = os.environ.get(
    "BW_UPLOAD_FOLDER",
    os.path.join(BASE_DIR, "app", "static", "uploads"),
)

# Custom and restored favicons. Defaults to the bundled static favicon folder,
# but portable builds can redirect this to a persistent data directory while
# keeping built-in preset favicons available from app/static/favicons.
FAVICON_UPLOAD_FOLDER = os.environ.get(
    "BW_FAVICON_UPLOAD_FOLDER",
    os.path.join(BASE_DIR, "app", "static", "favicons"),
)


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except (TypeError, ValueError):
        __import__("sys").stderr.write(
            f"WARNING: {name} is not a positive integer; "
            f"falling back to default ({default}).\n"
        )
        return default


BACKGROUND_IMAGE_MAX_UPLOAD_SIZE = _positive_int_env(
    "BW_BACKGROUND_IMAGE_MAX_UPLOAD_SIZE_BYTES",
    4 * 1024 * 1024,
)
BACKGROUND_IMAGE_MAX_PIXELS = _positive_int_env(
    "BW_BACKGROUND_IMAGE_MAX_PIXELS",
    16_000_000,
)
BACKGROUND_IMAGE_MAX_DIMENSION = _positive_int_env(
    "BW_BACKGROUND_IMAGE_MAX_DIMENSION",
    2560,
)
_DEFAULT_MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
try:
    MAX_CONTENT_LENGTH = int(os.environ.get(
        "BW_MAX_CONTENT_LENGTH_BYTES",
        str(_DEFAULT_MAX_CONTENT_LENGTH),
    ))
    if MAX_CONTENT_LENGTH <= 0:
        raise ValueError("must be positive")
except (TypeError, ValueError):
    # Malformed env var: fall back to the default rather than crash at boot.
    import sys as _sys
    print(
        "WARNING: BW_MAX_CONTENT_LENGTH_BYTES is not a positive integer; "
        "falling back to default (16 MB).",
        file=_sys.stderr,
    )
    MAX_CONTENT_LENGTH = _DEFAULT_MAX_CONTENT_LENGTH
# Note: SVG is intentionally disallowed due to potential embedded script/XSS risks.
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# Page Attachments
# Stored outside the static folder so they are served through an authenticated
# route and not directly accessible by URL.
ATTACHMENT_FOLDER = os.environ.get(
    "BW_ATTACHMENT_FOLDER",
    os.path.join(BASE_DIR, "instance", "attachments"),
)
MAX_ATTACHMENT_SIZE = int(os.environ.get(
    "BW_MAX_ATTACHMENT_SIZE_BYTES",
    str(100 * 1024 * 1024),
))  # 100 MB default per attachment
# Executable source extensions (.py / .js / .ts) are deliberately *not* in
# the default allow-list.  Although attachments are served with
# ``Content-Disposition: attachment`` (so browsers will not execute them
# in-context), a user could still be tricked into running a malicious
# script downloaded from a wiki they trust.  Admins who genuinely need to
# share source files can re-add these extensions via Admin → Settings.
#
# Base set shared by page-attachment, chat-attachment, and kanban-attachment
# allow-lists to ensure a single source of truth.  Feature-specific
# extensions (audio, video, joke-audio) are added on top.
_BASE_ALLOWED_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "md", "csv", "json", "xml", "zip", "tar", "gz",
    "png", "jpg", "jpeg", "gif", "webp",
})

# Audio and joke-audio extensions.  .mp5 / .mp7 are handled by
# helpers._joke_audio which transparently converts them to .mp3.
_AUDIO_EXTENSIONS = {"mp3", "ogg"}
_JOKE_AUDIO_EXTENSIONS = {"mp5", "mp7"}
_VIDEO_EXTENSIONS = {"mp4", "webm"}

ATTACHMENT_ALLOWED_EXTENSIONS = (
    _BASE_ALLOWED_EXTENSIONS
    | _VIDEO_EXTENSIONS
    | _AUDIO_EXTENSIONS
    | _JOKE_AUDIO_EXTENSIONS
)
PLATFORM_UPLOAD_BLACKLIST = os.environ.get("BW_PLATFORM_UPLOAD_BLACKLIST", "")

# Chat Attachments
# Stored outside the static folder (authenticated download route).
CHAT_ATTACHMENT_FOLDER = os.environ.get(
    "BW_CHAT_ATTACHMENT_FOLDER",
    os.path.join(BASE_DIR, "instance", "chat_attachments"),
)
# Chat does not support video; audio and joke-audio are still accepted.
CHAT_ALLOWED_EXTENSIONS = (
    _BASE_ALLOWED_EXTENSIONS
    | _AUDIO_EXTENSIONS
    | _JOKE_AUDIO_EXTENSIONS
)

# Note: chat attachment size limits, daily attachment quotas, and cleanup schedule
# are now fully managed via Admin → Settings in the web interface.

# Kanban Ticket Attachments
# Stored outside the static folder so they are served through an authenticated
# route and not directly accessible by URL.
KANBAN_ATTACHMENT_FOLDER = os.environ.get(
    "BW_KANBAN_ATTACHMENT_FOLDER",
    os.path.join(BASE_DIR, "instance", "kanban_attachments"),
)
KANBAN_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MB per attachment
# See note on ``ATTACHMENT_ALLOWED_EXTENSIONS`` above: same rationale
# applies to kanban ticket attachments.
KANBAN_ATTACHMENT_ALLOWED_EXTENSIONS = (
    _BASE_ALLOWED_EXTENSIONS
    | _VIDEO_EXTENSIONS
    | _AUDIO_EXTENSIONS
    | _JOKE_AUDIO_EXTENSIONS
)

# Custom Page Files
# Stored outside the static folder (authenticated admin upload + public serving).
CUSTOM_PAGE_FILES_FOLDER = os.environ.get(
    "BW_CUSTOM_PAGE_FILES_FOLDER",
    os.path.join(BASE_DIR, "instance", "custom_page_files"),
)
CUSTOM_PAGE_MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MB per file
CUSTOM_PAGE_MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100 MB per video (overridable via site settings)

# Text-to-Speech (built-in `tts` plugin)
# Cached audio generated by the TTS plugin lives outside the static folder so
# they are served through the authenticated ``/page/<slug>/tts/audio`` and
# ``/page/<slug>/tts/download`` routes (which apply the same visibility checks
# as the page itself).  The folder is created automatically by the plugin's
# ``on_load`` hook the first time it runs.
TTS_FOLDER = os.environ.get(
    "BW_TTS_FOLDER",
    os.path.join(INSTANCE_DIR, "tts"),
)
# Web requests enqueue durable TTS rows only. Run a separate worker process:
#   python scripts/tts_worker.py
# Inline generation is kept as an explicit test/dev escape hatch.
# Optional env knobs:
#   BW_TTS_INLINE_WORKER               default 0; set 1 only for tests/dev
#   BW_TTS_WORKER_COUNT                default 1, capped at 8
#   BW_TTS_MIN_START_INTERVAL_SECONDS  default 0.25 for real backends, 0 for stub tests
#   BW_TTS_MANUAL_MAX_ACTIVE_JOBS      default 1 pending/processing manual job
#   BW_TTS_MANUAL_MAX_ACTIVE_PER_USER  default 1 pending/processing job per user
#   BW_TTS_BACKEND                     default piper; stub is reserved for tests
#   BW_TTS_PIPER_VOICE_DIR             default instance/piper-voices
#   BW_TTS_PIPER_AUTO_DOWNLOAD         default 1; downloads voice models only, never page text
#   BW_TTS_PIPER_VOICE_MAP             optional JSON or code=voice CSV overrides
TTS_MANUAL_MAX_ACTIVE_JOBS = _positive_int_env(
    "BW_TTS_MANUAL_MAX_ACTIVE_JOBS",
    1,
)
TTS_MANUAL_MAX_ACTIVE_PER_USER = _positive_int_env(
    "BW_TTS_MANUAL_MAX_ACTIVE_PER_USER",
    1,
)

# Logging
# Logging level controls the verbosity of logs
# Available levels:
#   - "off":      No logging at all
#   - "minimal":  Only critical errors and warnings
#   - "medium":   Errors, warnings, and important actions (logins, signups, admin actions)
#   - "verbose":  Medium + all user actions (page edits, profile changes, etc.)
#   - "debug":    All of the above + every HTTP request and detailed debug info
# Default: "verbose" (highest useful level for production)
# Override at runtime with BW_LOGGING_LEVEL=<off|minimal|medium|verbose|debug>
_VALID_LOGGING_LEVELS = {"off", "minimal", "medium", "verbose", "debug"}
LOGGING_LEVEL = os.environ.get("BW_LOGGING_LEVEL", "verbose").strip().lower()
if LOGGING_LEVEL not in _VALID_LOGGING_LEVELS:
    __import__("sys").stderr.write(
        f"WARNING: BW_LOGGING_LEVEL={LOGGING_LEVEL!r} is not valid; "
        f"expected one of {_VALID_LOGGING_LEVELS}. "
        f"Falling back to 'verbose'.\n"
    )
    LOGGING_LEVEL = "verbose"

# Log file path
LOG_FILE = os.environ.get(
    "BW_LOG_FILE",
    os.path.join(BASE_DIR, "logs", "bananawiki.log"),
)

# Page History
# Enable or disable the page history viewer. When True (the default), every
# edit, title change, and revert is recorded and can be viewed or reverted.
# Reverting creates a new history entry: nothing is ever deleted from history.
# Set to False to hide all history routes (/history, /revert, etc.) from users.
PAGE_HISTORY_ENABLED = True

# Experimental Obsidian Sync
# Enable the local Obsidian pull/push integration. The sync script reuses the
# existing BananaWiki user accounts and only allows editor/admin roles.
EXPERIMENTAL_OBSIDIAN_SYNC = False

# EasyWiki Mode
# When True the wiki runs in minimal-feature mode: chat, kanban, canvas,
# TTS, assessments, badges, and other advanced plugins are disabled.
# Set via BW_EASY_WIKI=1 environment variable by the hosting platform.
# Standalone deployments (app.py) never set this. They always run the
# full BananaWiki feature set.
#
# The runtime check lives in helpers._server_restart.is_easy_wiki().
# config.EASY_WIKI intentionally omitted. No consumer reads it as a
# module constant; the hosting platform's env-var injection plus the
# helper function is the single source of truth.

# Invite Codes
# How long invite codes last before expiring (in hours)
INVITE_CODE_EXPIRY_HOURS = 48

# Page Reservations
# How long a page reservation lasts before expiring (in hours)
PAGE_RESERVATION_DURATION_HOURS = 48

# Cooldown period after a reservation ends before user can re-reserve (in hours)
PAGE_RESERVATION_COOLDOWN_HOURS = 24

# Server Restart
# Minimum interval between admin-triggered server restarts (in seconds).
#
# This is a hard, **code-only** cooldown: there is no UI / DB / env-var
# override on purpose.  The only way to change it is to edit this constant
# and redeploy BananaWiki.  Keeping it in code (not in the database) means a
# compromised admin account cannot simply turn the cooldown off and use the
# restart button as a denial-of-service primitive against the wiki.
#
# The cooldown is enforced **per wiki**: restarting `wiki.domain.com` does
# not affect any sibling wiki on the same machine, and the cooldown stored in
# `site_settings.last_server_restart_at` is local to that wiki's database.
SERVER_RESTART_COOLDOWN_SECONDS = 60

# Migration Import Limits
# Maximum total uncompressed size of a migration import ZIP (bytes).
# Prevents ZIP-bomb / memory-exhaustion attacks where a small compressed file
# decompresses into a much larger payload.
MAX_IMPORT_UNCOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB

# Maximum uncompressed size of any single member inside the ZIP.
MAX_IMPORT_MEMBER_SIZE = 200 * 1024 * 1024  # 200 MB

# Maximum number of file entries inside a migration ZIP.  Protects against
# pathological archives that pass the byte-size checks but contain millions of
# tiny entries (inode exhaustion, wall-clock time exhaustion).
MAX_IMPORT_FILE_COUNT = 50_000

# Site migration export tuning.  Large wikis should export a raw SQLite
# snapshot without also materialising a huge site_export.json in memory.
SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES = _positive_int_env(
    "BW_SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES",
    128 * 1024 * 1024,
)
SITE_EXPORT_TEMP_DIR = os.environ.get(
    "BW_SITE_EXPORT_TEMP_DIR",
    os.path.join(INSTANCE_DIR, "tmp_exports"),
)
SITE_EXPORT_COMPRESS_LEVEL = _positive_int_env(
    "BW_SITE_EXPORT_COMPRESS_LEVEL",
    1,
)
SITE_EXPORT_STORE_FILE_BYTES = _positive_int_env(
    "BW_SITE_EXPORT_STORE_FILE_BYTES",
    64 * 1024 * 1024,
)
SITE_EXPORT_STORE_EXTENSIONS = os.environ.get(
    "BW_SITE_EXPORT_STORE_EXTENSIONS",
    ",".join([
        ".7z", ".avi", ".bz2", ".dmg", ".docx", ".flac", ".gif", ".gz",
        ".heic", ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4",
        ".ogg", ".pdf", ".png", ".pptx", ".rar", ".tar", ".tgz", ".webm",
        ".webp", ".xlsx", ".xz", ".zip", ".zst",
    ]),
)

# Plugin Import Limits
# Maximum total uncompressed size of a .bwplugin ZIP (bytes).
# Prevents ZIP-bomb / disk-exhaustion attacks.
MAX_PLUGIN_UNCOMPRESSED_SIZE = 50 * 1024 * 1024  # 50 MB

# Maximum number of file entries inside a .bwplugin ZIP.
MAX_PLUGIN_FILE_COUNT = 5_000
MAX_PLUGIN_SINGLE_FILE_SIZE = 10 * 1024 * 1024
MAX_PLUGIN_COMPRESSION_RATIO = 200

# Retired Telegram backup compatibility constants
# Retained for old plugins and migrations. Telegram backup delivery is disabled;
# managed installations use the optional repository backup commands.
SYNC = False
SYNC_SPLIT_THRESHOLD = 19 * 1024 * 1024  # 19 MB
SYNC_COMPRESS_LEVEL = 9
SYNC_INCLUDE_CHAT_ATTACHMENTS = True

# Platform OAuth SSO
# These environment variables are injected by the hosting platform's instance
# manager when the platform OAuth SSO feature is enabled.  The wiki-side
# consumer in ``routes/platform_oauth.py`` reads them directly from
# ``os.environ`` rather than through config.py because they are set by the
# hosting platform and are not relevant for standalone (non-hosted) deployments.
#
# Listed here for discoverability:
#
#   BW_PLATFORM_OAUTH_ENABLED         "1" when platform OAuth SSO is active
#   BW_PLATFORM_OAUTH_CLIENT_ID       OAuth client ID for this instance
#   BW_PLATFORM_OAUTH_CLIENT_SECRET   OAuth client secret (raw, injected at spawn)
#   BW_PLATFORM_OAUTH_PORTAL_BASE     Base URL of the hosting portal
#   BW_PLATFORM_OAUTH_AUTHORIZE_URL   Full OAuth authorize endpoint
#   BW_PLATFORM_OAUTH_TOKEN_URL       Full OAuth token endpoint
#   BW_PLATFORM_OAUTH_USERINFO_URL    Full OAuth userinfo endpoint
#   BW_PLATFORM_OAUTH_VERIFY_URL      Full OAuth verify endpoint
#   BW_PLATFORM_OAUTH_LINK_URL        Full account link endpoint
#   BW_PLATFORM_OAUTH_UNLINK_URL      Full account unlink endpoint
#   BW_PLATFORM_OAUTH_LINK_STATUS_URL Full link-status endpoint
#   BW_PLATFORM_INSTANCE_ID           This instance's ID in the hosting DB

# Point this at the corresponding source when deploying a modified version.
SOURCE_CODE_URL = os.environ.get("BW_SOURCE_URL", "https://github.com/BananaSuite/BananaWiki").strip()
from urllib.parse import urlsplit as _source_urlsplit
_source_parts = _source_urlsplit(SOURCE_CODE_URL)
if _source_parts.scheme not in {"http", "https"} or not _source_parts.netloc or _source_parts.username:
    raise ValueError("BW_SOURCE_URL must be an absolute HTTP(S) source URL")
