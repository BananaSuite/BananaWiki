"""Hosted instance environment, upload policy, and platform integration."""

import logging
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone
from . import config
from .db import (
    get_account_by_id,
    get_hosting_settings,
    get_hosting_db_context,
)
from .instance_paths import (
    _instance_bind_host,
)

logger = logging.getLogger(__name__)


def _positive_int_env(name, default, *, minimum=1, maximum=None):
    """Return a bounded positive integer from an environment variable."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _positive_float_env(name, default, *, minimum=0.1, maximum=None):
    """Return a bounded positive float from an environment variable."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


# ---------------------------------------------------------------------------
# Tenant path safety.
#
# A tenant container mounts its own data directory read-write at /data, and a
# wiki admin may run plugin code in there, so the tenant can create any file,
# link or FIFO inside that directory.  The portal runs on the host and opens
# paths in the same directory with its own privileges, so it has to treat
# every one of them as hostile.  A link it follows could make it read or write
# the platform database, the portal's keys or another tenant's files, and a
# FIFO it opens could block a portal thread for good.
#
# The data directory itself is trusted: the portal creates it under
# INSTANCES_DIR, and the tenant cannot rename or replace the root of its own
# mount.  Only what sits inside it is tenant-controlled.  The helpers below
# therefore open entries relative to that directory without following links,
# check what they opened, and never pass SQLite a name they have not checked.
# ---------------------------------------------------------------------------

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK makes opening a FIFO fail or return at once instead of waiting
# for the other end.  It has no effect on regular files.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# SQLite opens these next to a database.  It refuses to follow a link there
# on its own, but it would still open a FIFO, and a FIFO at "-journal" hangs
# even a read.
_TENANT_DB_SIDECARS = ("-wal", "-shm", "-journal")


def _reject_link_or_nonregular(path):
    """lstat *path* and refuse a symlink or a non-regular file.

    ``lstat`` never follows the final component, so a symlink is caught here
    rather than silently resolved.  Returns the ``os.stat_result`` on success.
    """
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"Refusing to follow a symbolic link at {path}")
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"Refusing a non-regular file at {path}")
    return st


def _assert_path_within(base_dir, path):
    """Refuse *path* unless it resolves inside *base_dir*.

    ``realpath`` resolves every symlink in the path, so a link anywhere in the
    tenant-controlled portion that points outside the instance directory is
    rejected here.
    """
    base = os.path.realpath(base_dir)
    resolved = os.path.realpath(path)
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"Path {path} escapes the instance directory {base}")


def guard_tenant_path(base_dir, path):
    """Check a path under a tenant data directory before the portal uses it.

    Refuses symlinks and non-regular files at *path* and refuses paths that
    resolve outside *base_dir*.  Returns the ``os.stat_result`` for *path*.
    Callers pass the instance's own data directory as *base_dir*.

    This is a check, not an open: a running tenant can swap the entry right
    after it.  Use :func:`open_tenant_file` or :func:`connect_tenant_db` to
    actually read or write, which also hold against that race.
    """
    _assert_path_within(base_dir, path)
    return _reject_link_or_nonregular(path)


def open_tenant_file(base_dir, path, *, max_bytes=None):
    """Open a regular file under a tenant data directory for reading.

    Returns an OS file descriptor the caller must close.  The parent folders
    are opened one by one with :func:`open_tenant_dir` and the file relative
    to the last of them with ``O_NOFOLLOW``, so no link anywhere on the way is
    followed, even one the tenant swaps in after an earlier check.
    ``O_NONBLOCK`` keeps a FIFO from blocking the open, and the type check
    runs on the descriptor that was actually opened.  With *max_bytes*, a
    larger file is refused before anything is read.
    """
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base_dir))
    if rel == os.curdir or rel.startswith(os.pardir) or os.path.isabs(rel):
        raise ValueError(f"Path {path} is not inside the instance directory {base_dir}")
    parent, name = os.path.split(rel)
    dir_fd = open_tenant_dir(base_dir, parent.replace(os.sep, "/"))
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Refusing a non-regular file at {path}")
        if max_bytes is not None and st.st_size > max_bytes:
            raise ValueError(f"Refusing an oversized file at {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_tenant_file(base_dir, path, *, max_bytes):
    """Return the bytes of a small regular file under a tenant data directory."""
    fd = open_tenant_file(base_dir, path, max_bytes=max_bytes)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(max_bytes + 1)[:max_bytes]


def write_tenant_file(data_dir, name, text):
    """Replace the file *name* directly inside *data_dir* with *text*.

    Used for the files the portal itself keeps in a tenant directory (PID
    files, the start lock).  Writing through ``open(path, "w")`` would follow
    a link the tenant planted under that name and truncate whatever it points
    at.  Instead the text goes into a fresh temporary file, created with
    ``O_EXCL`` so it cannot be a link, and ``os.replace`` then swaps that file
    in: a rename replaces a link itself and never writes through it.
    """
    if not name or os.sep in name or name in (".", ".."):
        raise ValueError(f"Invalid tenant file name: {name!r}")
    path = os.path.join(data_dir, name)
    fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def _guard_tenant_db_files(data_dir, db_path):
    """Refuse a tenant database whose file or SQLite sidecars look planted.

    The database must be a regular file inside *data_dir*.  A ``-wal``,
    ``-shm`` or ``-journal`` file may be missing, but if it exists it must be
    a regular file too.
    """
    guard_tenant_path(data_dir, db_path)
    for suffix in _TENANT_DB_SIDECARS:
        sidecar = db_path + suffix
        if os.path.lexists(sidecar):
            _reject_link_or_nonregular(sidecar)


def connect_tenant_db(data_dir, db_path, *, read_only=False, timeout=20, max_seconds=120):
    """Open a tenant's SQLite database without following a planted link.

    SQLite resolves a symlinked database name before it opens the file and
    reports the file it really opened in ``PRAGMA database_list``.  So after
    the usual checks this compares that name with the path we meant; a link
    swapped in after the checks shows up there and the connection is closed
    before any statement touches the file.  SQLite already refuses to follow
    links for its ``-wal`` and ``-journal`` files.

    The schema is tenant-written too, so the connection runs with triggers
    and views disabled and ``trusted_schema`` off, and statements are
    interrupted once *max_seconds* have passed since it was opened.  The
    wiki defines no triggers or views.  A trigger a tenant adds could
    silently cancel the portal's recovery writes (password resets, token
    revocation, plugin quarantine), and a recursive view named like a wiki
    table could keep a portal thread busy forever.  Raises ``ValueError``
    when the path is not safe to open.
    """
    _guard_tenant_db_files(data_dir, db_path)
    expected = os.path.join(
        os.path.realpath(os.path.dirname(db_path)), os.path.basename(db_path)
    )
    if read_only:
        conn = sqlite3.connect(
            pathlib.Path(expected).as_uri() + "?mode=ro", uri=True, timeout=timeout
        )
    else:
        conn = sqlite3.connect(expected, timeout=timeout)
    try:
        opened = None
        for _seq, schema, filename in conn.execute("PRAGMA database_list"):
            if schema == "main":
                opened = filename
        if opened != expected:
            raise ValueError(
                f"Refusing tenant database {db_path}: SQLite opened {opened!r}"
            )
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        deadline = time.monotonic() + max_seconds
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
        return conn
    except BaseException:
        conn.close()
        raise


def snapshot_tenant_db(data_dir, db_path, dest_path, *, timeout=300):
    """Write a consistent, checked copy of a tenant database to *dest_path*.

    Reads through :func:`connect_tenant_db`, so a running tenant cannot point
    the copy at another file.  The copy is built in a private temporary file
    next to *dest_path*, must pass ``PRAGMA quick_check``, and is then linked
    into place, so *dest_path* never holds a partial copy and an existing file
    there is never replaced.  Returns *dest_path*.
    """
    dest_dir = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    if os.path.lexists(dest_path):
        raise FileExistsError(f"Snapshot destination already exists: {dest_path}")
    source_size = guard_tenant_path(data_dir, db_path).st_size
    if shutil.disk_usage(dest_dir).free < source_size + 64 * 1024 * 1024:
        raise OSError("Not enough free space for a verified database snapshot.")
    deadline = time.monotonic() + timeout

    def _progress(*_args):
        if time.monotonic() >= deadline:
            raise TimeoutError("Database snapshot exceeded its time limit.")

    fd, tmp_path = tempfile.mkstemp(prefix=".tenant-snapshot-", suffix=".db", dir=dest_dir)
    os.close(fd)
    try:
        src = connect_tenant_db(
            data_dir, db_path, read_only=True, timeout=5, max_seconds=timeout
        )
        try:
            dst = sqlite3.connect(tmp_path, timeout=5)
            try:
                src.backup(dst, pages=256, progress=_progress, sleep=0.01)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "The database snapshot failed its integrity check."
                    )
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp_path, "rb") as done:
            os.fsync(done.fileno())
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        # link() fails if the name exists, so a concurrent snapshot with the
        # same name is never overwritten.
        os.link(tmp_path, dest_path)
        return dest_path
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(tmp_path + suffix)
            except FileNotFoundError:
                pass


def open_tenant_dir(data_dir, relpath):
    """Open a directory below *data_dir* without following any link on the way.

    Each component of *relpath* is opened relative to the previous one with
    ``O_NOFOLLOW``, so a tenant cannot redirect the walk by swapping a parent
    directory for a link.  Returns a directory descriptor the caller must
    close.  Raises ``OSError`` when a component is a link or not a directory.
    """
    parts = [part for part in relpath.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Invalid tenant directory: {relpath!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC
    fd = os.open(data_dir, flags)
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def iter_tenant_files(dir_fd, *, skip_hidden_and_tts=False, skip_dirs=()):
    """Yield ``(relative_path, file_fd, stat_result)`` for regular files under *dir_fd*.

    Walks with :func:`os.fwalk`, which keeps a descriptor for each directory
    and does not descend into linked directories.  Each file is opened
    relative to its directory's descriptor with ``O_NOFOLLOW`` and
    ``O_NONBLOCK`` and yielded only when the opened descriptor is a regular
    file; links, FIFOs, sockets and devices are skipped.  The caller must
    close each yielded descriptor.  With *skip_hidden_and_tts*, directories
    whose names start with a dot and ``tts`` caches are left out, as exports
    have always done.  Directories named in *skip_dirs* are not entered.
    """
    for dirpath, dirnames, filenames, walk_fd in os.fwalk(
        ".", dir_fd=dir_fd, follow_symlinks=False
    ):
        if skip_hidden_and_tts:
            dirnames[:] = [d for d in dirnames if d != "tts" and not d.startswith(".")]
        if skip_dirs:
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            try:
                fd = os.open(
                    fname, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=walk_fd
                )
            except OSError:
                # A link (ELOOP), a vanished file, or a FIFO with no writer.
                continue
            try:
                st = os.fstat(fd)
            except OSError:
                os.close(fd)
                continue
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                continue
            rel = os.path.normpath(os.path.join(dirpath, fname)).replace(os.sep, "/")
            yield rel, fd, st


# ---------------------------------------------------------------------------
# Host-owned per-instance state (recovery snapshots, plugin quarantine).
#
# Kept under HOSTING_PLATFORM_STATE_DIR, outside every tenant mount, because
# anything inside a tenant's data directory can be rewritten by that tenant.
# ---------------------------------------------------------------------------

_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def platform_state_dir(instance_id):
    """Return the host-owned state directory for one instance.

    Refuses an id that is not a plain token, and a state directory that sits
    inside INSTANCES_DIR, where a tenant could reach it.
    """
    instance_id = str(instance_id)
    if not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError(f"Invalid instance id: {instance_id!r}")
    root = os.path.realpath(config.HOSTING_PLATFORM_STATE_DIR)
    instances = os.path.realpath(config.INSTANCES_DIR)
    if root == instances or root.startswith(instances + os.sep):
        raise RuntimeError(
            "HOSTING_PLATFORM_STATE_DIR must not be inside INSTANCES_DIR"
        )
    return os.path.join(root, instance_id)


def plugin_snapshot_dir(instance_id):
    """Return the host-owned directory holding platform plugin snapshots."""
    return os.path.join(platform_state_dir(instance_id), "plugin_snapshots")


def _plugin_quarantine_marker(instance_id):
    return os.path.join(platform_state_dir(instance_id), "plugin_quarantine")


def instance_plugins_quarantined(instance_id):
    """Return True when the operator has quarantined this tenant's plugins.

    The marker is stored host-side so a tenant restart cannot lift it; while it
    is set the tenant starts with external plugins disabled.
    """
    if instance_id is None:
        return False
    try:
        return os.path.isfile(_plugin_quarantine_marker(instance_id))
    except (OSError, ValueError):
        return False


def set_instance_plugin_quarantine(instance_id, active):
    """Set or clear the host-side plugin-quarantine marker for *instance_id*."""
    marker = _plugin_quarantine_marker(instance_id)
    if active:
        state_dir = os.path.dirname(marker)
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".plugin_quarantine.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(datetime.now(timezone.utc).isoformat() + "\n")
            os.replace(tmp_path, marker)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    else:
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass


def _row_value(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    return default


def _normalize_upload_extensions(text):
    """Return a canonical comma-separated extension list."""
    if not text:
        return ""
    values = set()
    for raw in str(text).split(","):
        ext = raw.strip().lower().lstrip(".")
        if re.fullmatch(r"[a-z0-9]+", ext):
            values.add(ext)
    return ", ".join(sorted(values))


def _combined_upload_blocklist(*parts):
    values = set()
    for part in parts:
        normalized = _normalize_upload_extensions(part)
        if normalized:
            values.update(
                item.strip() for item in normalized.split(",") if item.strip()
            )
    return ", ".join(sorted(values))


def _bounded_upload_size_mb(value, default=None):
    if default is None:
        default = config.INSTANCE_MAX_ATTACHMENT_MB
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = int(default)
    return max(1, min(2048, size))


def get_effective_instance_upload_policy(inst=None):
    """Return the upload policy enforced for a hosted wiki instance."""
    settings = None
    try:
        settings = get_hosting_settings()
    except Exception:
        logger.exception("Failed to read hosting settings for upload policy")

    global_size = _bounded_upload_size_mb(
        _row_value(
            settings,
            "global_wiki_upload_max_size_mb",
            config.INSTANCE_MAX_ATTACHMENT_MB,
        ),
        default=config.INSTANCE_MAX_ATTACHMENT_MB,
    )
    instance_size = _row_value(inst, "upload_max_size_mb")
    size_mb = (
        _bounded_upload_size_mb(instance_size, default=global_size)
        if instance_size
        else global_size
    )
    global_blocked = _row_value(settings, "global_wiki_blocked_extensions", "")
    instance_blocked = _row_value(inst, "upload_blocked_extensions", "")
    return {
        "upload_max_size_mb": size_mb,
        "blocked_extensions": _combined_upload_blocklist(
            global_blocked, instance_blocked
        ),
    }


def _apply_upload_policy_to_instance_db(db_path, policy):
    """Write the host-enforced upload policy into one wiki database."""
    if not db_path or not os.path.isfile(db_path):
        return False
    conn = None
    try:
        # The DB sits in the tenant's writable data dir and the tenant may be
        # running, so open it without following a link it planted there.
        conn = connect_tenant_db(os.path.dirname(db_path), db_path)
        conn.execute("INSERT OR IGNORE INTO site_settings (id) VALUES (1)")
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(site_settings)").fetchall()
        }
        if "upload_max_size_mb" not in cols:
            conn.execute(
                "ALTER TABLE site_settings ADD COLUMN upload_max_size_mb INTEGER NOT NULL DEFAULT 100"
            )
        if "upload_mode" not in cols:
            conn.execute(
                "ALTER TABLE site_settings ADD COLUMN upload_mode TEXT NOT NULL DEFAULT 'allow_all'"
            )
        if "upload_whitelist" not in cols:
            conn.execute(
                "ALTER TABLE site_settings ADD COLUMN upload_whitelist TEXT NOT NULL DEFAULT ''"
            )
        if "platform_upload_blacklist" not in cols:
            conn.execute(
                "ALTER TABLE site_settings ADD COLUMN platform_upload_blacklist TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE site_settings SET upload_max_size_mb=?, platform_upload_blacklist=? WHERE id=1",
            (
                int(policy["upload_max_size_mb"]),
                policy["blocked_extensions"],
            ),
        )
        conn.execute(
            """UPDATE site_settings
               SET upload_mode='allow_all'
               WHERE id=1
                 AND upload_mode='whitelist'
                 AND COALESCE(upload_whitelist, '')=''"""
        )
        conn.commit()
        return True
    except ValueError:
        logger.warning(
            "Not applying the upload policy to %s: the tenant database path is "
            "not a regular file inside its data directory", db_path,
        )
        return False
    except Exception:
        logger.exception("Failed to apply upload policy to %s", db_path)
        return False
    finally:
        if conn is not None:
            conn.close()


def _get_or_create_instance_oauth_credentials(inst, port):
    """Generate OAuth client credentials for an instance, if platform OAuth is enabled.

    If the instance already has a client_id, returns the stored
    (client_id, raw_secret).  Otherwise generates new credentials, stores
    the hash in the database, and returns the raw secret for env injection.

    Returns ``(client_id, client_secret)`` or ``(None, None)`` when OAuth
    is disabled or the instance row is missing.

    The raw client_secret is returned only once (at creation time) so the
    caller must inject it into the instance's environment immediately.
    """
    if inst is None:
        return None, None
    try:
        hs = get_hosting_settings()
        if not hs or not hs.get("platform_oauth_enabled"):
            return None, None
    except Exception:
        return None, None

    import hashlib, secrets, string as _string

    if inst.get("oauth_client_id") and inst.get("oauth_client_secret_hash"):
        return inst["oauth_client_id"], None

    instance_id = inst["id"]
    client_id = "bw_" + "".join(
        secrets.choice(_string.ascii_lowercase + _string.digits) for _ in range(28)
    )
    raw_secret = secrets.token_urlsafe(48)
    salt = secrets.token_hex(16)
    secret_hash = (
        salt + "$" + hashlib.sha256((salt + raw_secret).encode("utf-8")).hexdigest()
    )

    try:
        with get_hosting_db_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT oauth_client_id, oauth_client_secret_hash "
                "FROM instances WHERE id=?",
                (instance_id,),
            ).fetchone()
            if existing and existing["oauth_client_id"]:
                conn.execute("ROLLBACK")
                return existing["oauth_client_id"], None
            conn.execute(
                "UPDATE instances SET oauth_client_id=?, oauth_client_secret_hash=? "
                "WHERE id=?",
                (client_id, secret_hash, instance_id),
            )
            conn.commit()
        return client_id, raw_secret
    except Exception:
        logger.exception(
            "Failed to create OAuth credentials for instance %s", instance_id
        )
        return None, None


def _build_oauth_portal_base_url():
    """Return the base URL of the hosting portal for OAuth redirects."""
    if config.HOSTING_MODE == "port":
        scheme = config.HOSTING_PUBLIC_SCHEME
        host = config.HOSTING_PUBLIC_HOST
        port = config.HOSTING_PORT
        if port and port not in (80, 443):
            return f"{scheme}://{host}:{port}"
        return f"{scheme}://{host}"
    scheme = "https"
    domain = config.EFFECTIVE_PORTAL_DOMAIN
    return f"{scheme}://{domain}"


def _instance_env(data_dir, port, inst=None):
    """Build the environment dict for spawning a BananaWiki instance."""
    bind_host = _instance_bind_host()
    proxy = "0" if config.HOSTING_MODE == "port" else "1"
    upload_policy = get_effective_instance_upload_policy(inst)
    upload_size_bytes = int(upload_policy["upload_max_size_mb"]) * 1024 * 1024
    cookie_slug = "".join(
        c if c.isalnum() else "_"
        for c in os.path.basename(os.path.normpath(data_dir)).lower()
    ).strip("_") or str(port)
    owner = (
        get_account_by_id(inst["account_id"])
        if inst and inst.get("account_id")
        else None
    )
    is_unlimited_owner = bool(
        inst
        and (
            str(inst.get("domain_mode") or "hosting") == "apex"
            or (owner and owner.get("is_admin"))
        )
    )
    raw_storage_limit = inst.get("storage_limit_mb") if inst else None
    try:
        storage_limit_mb = (
            int(raw_storage_limit)
            if raw_storage_limit is not None
            else int(config.INSTANCE_STORAGE_LIMIT_MB)
        )
    except (TypeError, ValueError):
        storage_limit_mb = int(config.INSTANCE_STORAGE_LIMIT_MB)
    if is_unlimited_owner or storage_limit_mb <= 0:
        storage_limit_bytes = 0
    else:
        storage_limit_bytes = storage_limit_mb * 1024 * 1024

    # Portal credentials and its session/setup keys must never reach a tenant.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
        if key in os.environ
    }
    env.update(
        {
            "BW_SOURCE_URL": config.SOURCE_CODE_URL,
            "BW_PORT": str(port),
            "BW_HOST": bind_host,
            "BW_PROXY_MODE": proxy,
            "BW_DATABASE_PATH": os.path.join(data_dir, "bananawiki.db"),
            "BW_INSTANCE_DIR": data_dir,
            "BW_MAINTENANCE_FILE": os.path.join(data_dir, ".banana-maintenance"),
            "BW_UPLOAD_FOLDER": os.path.join(data_dir, "uploads"),
            "BW_ATTACHMENT_FOLDER": os.path.join(data_dir, "attachments"),
            "BW_CHAT_ATTACHMENT_FOLDER": os.path.join(data_dir, "chat_attachments"),
            "BW_KANBAN_ATTACHMENT_FOLDER": os.path.join(data_dir, "kanban_attachments"),
            "BW_CUSTOM_PAGE_FILES_FOLDER": os.path.join(data_dir, "custom_page_files"),
            "BW_LOG_FILE": os.path.join(data_dir, "bananawiki.log"),
            "BW_SITE_EXPORT_TEMP_DIR": os.path.join(data_dir, "tmp_exports"),
            "BW_MAX_CONTENT_LENGTH_BYTES": str(
                max(config.INSTANCE_MAX_UPLOAD_MB * 1024 * 1024, upload_size_bytes)
            ),
            "BW_MAX_ATTACHMENT_SIZE_BYTES": str(upload_size_bytes),
            "BW_PLATFORM_UPLOAD_BLACKLIST": upload_policy["blocked_extensions"],
            "BW_MANAGED_HOSTING": "1",
            # Process runtime never allows external plugins; the docker runtime
            # normally turns them on in container_runtime._container_env.  The
            # quarantine flag below keeps them off in both runtimes.
            "BW_ALLOW_EXTERNAL_PLUGINS": "0",
            "BW_STORAGE_LIMIT_BYTES": str(storage_limit_bytes),
            "BW_MEMORY_LIMIT_MB": str(max(128, int(config.INSTANCE_MEMORY_LIMIT_MB))),
            "BW_NOFILE_LIMIT": str(max(128, int(config.INSTANCE_NOFILE_LIMIT))),
            "BW_INSTANCE_STARTUP_TIMEOUT_SECONDS": str(
                config.INSTANCE_STARTUP_TIMEOUT_SECONDS
            ),
            # Give each instance a unique session cookie name so that cookies
            # from different instances on the same hostname (port mode) don't
            # collide.  Without this, all instances share the "bw_session"
            # cookie and each instance's secret key can't decrypt another's
            # cookie: causing CSRF errors ("Your session has expired").
            "BW_SESSION_COOKIE_NAME": f"bw_session_{cookie_slug}",
            # Do NOT set SECRET_KEY here: BananaWiki's _load_secret_key()
            # persists the key to BW_INSTANCE_DIR/.secret_key on first boot
            # and reloads it on subsequent starts.  Injecting a fresh random
            # key on every process start would invalidate every existing
            # session cookie whenever an instance is restarted.
        }
    )
    federation_instances = {
        value.strip() for value in os.environ.get("HOSTING_FEDERATION_INSTANCES", "").split(",")
        if value.strip()
    }
    env["BW_FEDERATION_ENABLED"] = "1" if inst and (
        "*" in federation_instances or str(inst["id"]) in federation_instances
    ) else "0"
    settings = get_hosting_settings()
    # Public wiki access is forbidden unless:
    #  - The global setting allows it (forbid_non_admin_public_wikis=0), OR
    #  - The instance owner is a platform admin, OR
    #  - The specific instance has been granted public_wiki_allowed=1
    owner_is_admin = bool(owner and owner.get("is_admin"))
    instance_allowed = bool(inst and inst.get("public_wiki_allowed"))
    global_forbids = not settings or bool(
        settings.get("forbid_non_admin_public_wikis", 1)
    )
    forbid_public = global_forbids and not owner_is_admin and not instance_allowed
    env["BW_FORBID_PUBLIC_MODE"] = "1" if forbid_public else "0"
    page_builder_allowed = bool(inst and inst.get("page_builder_allowed"))
    global_forbids_page_builder = not settings or bool(
        settings.get("forbid_non_admin_page_builder", 1)
    )
    forbid_page_builder = (
        global_forbids_page_builder and not owner_is_admin and not page_builder_allowed
    )
    env["BW_FORBID_PAGE_BUILDER"] = "1" if forbid_page_builder else "0"
    # Anonymous release of builder-authored pages is a separate fail-closed
    # boundary. It becomes available only when both restricted capabilities
    # are effectively approved for the tenant.
    env["BW_FORBID_PUBLIC_BUILDER_PAGES"] = (
        "1" if forbid_public or forbid_page_builder else "0"
    )

    # Inject OAuth credentials for platform SSO when enabled.
    oauth_client_id, oauth_client_secret = _get_or_create_instance_oauth_credentials(
        inst, port
    )
    if oauth_client_id:
        env["BW_PLATFORM_OAUTH_ENABLED"] = "1"
        env["BW_PLATFORM_OAUTH_CLIENT_ID"] = oauth_client_id
        if oauth_client_secret:
            env["BW_PLATFORM_OAUTH_CLIENT_SECRET"] = oauth_client_secret
        portal_base = _build_oauth_portal_base_url()
        env["BW_PLATFORM_OAUTH_PORTAL_BASE"] = portal_base
        env["BW_PLATFORM_OAUTH_AUTHORIZE_URL"] = f"{portal_base}/oauth/authorize"
        env["BW_PLATFORM_OAUTH_TOKEN_URL"] = f"{portal_base}/oauth/token"
        env["BW_PLATFORM_OAUTH_USERINFO_URL"] = f"{portal_base}/oauth/userinfo"
        env["BW_PLATFORM_OAUTH_VERIFY_URL"] = f"{portal_base}/oauth/verify"
        env["BW_PLATFORM_OAUTH_LINK_URL"] = f"{portal_base}/oauth/link"
        env["BW_PLATFORM_OAUTH_UNLINK_URL"] = f"{portal_base}/oauth/unlink"
        env["BW_PLATFORM_OAUTH_LINK_STATUS_URL"] = f"{portal_base}/oauth/link-status"
        env["BW_PLATFORM_INSTANCE_ID"] = inst["id"]
    else:
        env["BW_PLATFORM_OAUTH_ENABLED"] = "0"

    # Pass the instance expiry timestamp so the wiki can warn admins
    # before the hosting platform auto-terminates the instance.
    if inst and inst.get("expires_at"):
        env["BW_INSTANCE_EXPIRES_AT"] = inst["expires_at"]

    # Inject EasyWiki mode flag so the wiki app can strip advanced features.
    if inst and inst.get("easy_wiki"):
        env["BW_EASY_WIKI"] = "1"
    else:
        env["BW_EASY_WIKI"] = "0"

    # Inject TTS policy: disable generation for instances excluded by the
    # platform admin's whitelist / blacklist / global-off setting.
    tts_allowed = not settings or _hosting_tts_allowed_for_instance(settings, inst)
    if settings and not tts_allowed:
        env["BW_MANAGED_TTS_DISABLED"] = "1"
    else:
        env.pop("BW_MANAGED_TTS_DISABLED", None)

    # Hand the shared GPU TTS server to the wiki through its environment, and
    # only when the TTS policy allows this instance.  The URL and token used
    # to be copied into every tenant database, where a tenant admin could
    # export the token or point the URL at a server of their own; they are now
    # cleared from there (instance_database._apply_hosted_db_safety_defaults)
    # and a hosted wiki reads its GPU settings only from these variables.
    # BW_TTS_BACKEND is set as well so tenant images built before that change
    # still pick the remote backend.  In the docker runtime, plugin code in an
    # allowed tenant can still read its own environment, so the TTS policy is
    # what decides which tenants see the token.
    if (
        settings
        and tts_allowed
        and settings.get("global_tts_gpu_enabled")
        and (settings.get("global_tts_gpu_url") or "").strip()
        and (settings.get("global_tts_gpu_auth_token") or "").strip()
    ):
        env["BW_TTS_BACKEND"] = "remote-gpu"
        env["BW_TTS_REMOTE_GPU_URL"] = settings["global_tts_gpu_url"].strip().rstrip("/")
        env["BW_TTS_REMOTE_GPU_AUTH_TOKEN"] = settings["global_tts_gpu_auth_token"].strip()
        try:
            gpu_timeout = int(settings.get("global_tts_gpu_timeout") or 120)
        except (TypeError, ValueError):
            gpu_timeout = 120
        env["BW_TTS_REMOTE_GPU_TIMEOUT"] = str(gpu_timeout)

    # Forward the operator's plugin denylist (HOSTING_TENANT_PLUGIN_DENYLIST)
    # so the wiki refuses those ids too.  The wiki matches ids only, so this is
    # a policy switch, not a boundary: the same code under a new id passes.
    if config.HOSTING_TENANT_PLUGIN_DENYLIST:
        env["BW_MANAGED_PLUGIN_DENYLIST"] = config.HOSTING_TENANT_PLUGIN_DENYLIST

    # Keep external plugins off while the operator's plugin quarantine is in
    # force.  The marker lives host-side, outside the tenant mount, so neither
    # a restart nor the tenant can clear it, and container_runtime reads this
    # flag instead of switching external plugins on.  The wiki then does not
    # even scan its external plugin folder, whatever its database says.
    if inst and instance_plugins_quarantined(inst.get("id")):
        env["BW_ALLOW_EXTERNAL_PLUGINS"] = "0"
        env["BW_MANAGED_PLUGIN_QUARANTINE"] = "1"
    else:
        env["BW_MANAGED_PLUGIN_QUARANTINE"] = "0"

    return env


def _hosting_tts_allowed_for_instance(hosting_settings, inst):
    """Return True when the platform policy permits TTS for *inst*.

    Three modes:
    - ``global_tts_enabled=0`` → disabled for everyone.
    - ``global_tts_mode='all'``        → allowed for everyone (default).
    - ``global_tts_mode='whitelist'``  → only listed subdomains may generate.
    - ``global_tts_mode='blacklist'``  → everyone except listed subdomains.

    The subdomain list (``global_tts_list``) accepts one entry per line or
    comma-separated; values are case-insensitive.
    """
    if not hosting_settings.get("global_tts_enabled", 1):
        return False
    mode = (hosting_settings.get("global_tts_mode") or "all").strip().lower()
    if mode == "all":
        return True
    raw = hosting_settings.get("global_tts_list") or ""
    subdomains = {
        s.strip().lower() for s in raw.replace(",", "\n").splitlines() if s.strip()
    }
    subdomain = ((inst.get("subdomain") or "") if inst else "").strip().lower()
    if mode == "whitelist":
        return subdomain in subdomains
    if mode == "blacklist":
        return subdomain not in subdomains
    return True
