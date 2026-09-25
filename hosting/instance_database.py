"""Prepare, seed, and import hosted wiki databases."""

import logging
import os
import secrets
import sqlite3
import string
import subprocess
import sys
import tempfile
from helpers._passwords import generate_password_hash  # noqa: E402
from . import config
from . import container_runtime
from .instance_paths import (
    _instance_db_path,
)
from .instance_environment import (
    _positive_float_env,
    connect_tenant_db,
    guard_tenant_path,
)

logger = logging.getLogger(__name__)


_INSTANCE_DB_PREPARE_TIMEOUT = _positive_float_env(
    "BW_HOSTING_INSTANCE_DB_PREPARE_TIMEOUT_SECONDS",
    120.0,
    minimum=5.0,
    maximum=1800.0,
)


_HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START = os.environ.get(
    "BW_HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START", "1"
).strip().lower() not in {"0", "false", "no", "off"}


def _instance_db_prepare_env(data_dir):
    """Build the env used by subprocess DB preparation for one instance."""
    env = os.environ.copy()
    env.update(
        {
            "_BW_ROOT": config.BW_ROOT,
            "BW_DATABASE_PATH": _instance_db_path(data_dir),
            "BW_INSTANCE_DIR": data_dir,
            "BW_UPLOAD_FOLDER": os.path.join(data_dir, "uploads"),
            "BW_ATTACHMENT_FOLDER": os.path.join(data_dir, "attachments"),
            "BW_CHAT_ATTACHMENT_FOLDER": os.path.join(data_dir, "chat_attachments"),
            "BW_KANBAN_ATTACHMENT_FOLDER": os.path.join(data_dir, "kanban_attachments"),
            "BW_CUSTOM_PAGE_FILES_FOLDER": os.path.join(data_dir, "custom_page_files"),
            "BW_LOG_FILE": os.path.join(data_dir, "bananawiki.log"),
        }
    )
    return env


def _apply_hosted_db_safety_defaults(db_path):
    """Apply hosting-only defaults that keep old/large DBs from fanning out."""
    conn = None
    try:
        # The DB is in the tenant's writable data dir; open it without
        # following a link the tenant planted there.
        conn = connect_tenant_db(os.path.dirname(db_path), db_path)
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(site_settings)").fetchall()
        }
        if (
            _HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START
            and "tts_auto_generate_enabled" in cols
        ):
            conn.execute(
                "UPDATE site_settings SET tts_auto_generate_enabled=0 WHERE id=1"
            )

        # Never keep the shared platform GPU endpoint or token in a tenant
        # database.  Older portal builds copied them into every tenant, where
        # a tenant admin could read the token back through an export or point
        # the URL at a server of their own.  A hosted wiki now reads its GPU
        # settings only from the environment that
        # instance_environment._instance_env builds (and only for instances
        # the TTS policy allows), so clear whatever an older build left here.
        if "tts_gpu_url" in cols and "tts_gpu_enabled" in cols:
            row = conn.execute(
                "SELECT tts_gpu_url, tts_gpu_auth_token FROM site_settings WHERE id=1"
            ).fetchone()
            had_gpu_config = bool(
                row and ((row[0] or "").strip() or (row[1] or "").strip())
            )
            if had_gpu_config:
                conn.execute(
                    "UPDATE site_settings SET "
                    "tts_gpu_enabled=0, tts_gpu_url='', tts_gpu_auth_token='' "
                    "WHERE id=1"
                )

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "tts_generations" in tables and _HOSTING_DISABLE_TTS_AUTO_GENERATE_ON_START:
            conn.execute("DELETE FROM tts_generations")
        conn.commit()
        return True
    except Exception:
        logger.exception("Failed to apply hosted DB safety defaults to %s", db_path)
        return False
    finally:
        if conn is not None:
            conn.close()


def _tenant_container_running(data_dir):
    """Return True when this tenant's container is up right now."""
    return (
        config.HOSTING_INSTANCE_RUNTIME == "docker"
        and container_runtime.container_is_running(data_dir)
    )


def _run_instance_db_migrations(data_dir, db_path):
    """Run the wiki's own ``db.init_db()`` against one tenant DB in a subprocess."""
    script = "\n".join(
        [
            "import os, sys",
            "sys.path.insert(0, os.environ['_BW_ROOT'])",
            "import db",
            "db.init_db()",
        ]
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=config.BW_ROOT,
            env=_instance_db_prepare_env(data_dir),
            capture_output=True,
            text=True,
            timeout=_INSTANCE_DB_PREPARE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.exception(
            "Instance DB preparation timed out after %.1fs for %s",
            _INSTANCE_DB_PREPARE_TIMEOUT,
            db_path,
        )
        return False
    except Exception:
        logger.exception("Instance DB preparation failed for %s", db_path)
        return False

    if result.returncode != 0:
        logger.error(
            "Instance DB preparation failed for %s (rc=%s): %s",
            db_path,
            result.returncode,
            (result.stderr or "")[-2000:],
        )
        return False
    return True


def _prepare_instance_database(data_dir):
    """Run schema migrations and hosting safety defaults before instance start.

    Schema migrations are critical: if they fail we return ``False`` so
    the caller does not attempt to start Gunicorn against a broken DB.

    Hosting safety defaults (TTS fan-out limits, clearing platform GPU
    settings an older build copied in) are best-effort: a failure there is
    logged as a warning but does *not* prevent the instance from starting.
    They run even when the migrations fail, so a copied GPU token is still
    removed.  Gunicorn will run ``db.init_db()`` again during its own startup
    anyway.

    The database must be a regular file inside *data_dir*; a planted link or
    special file fails the preparation without being opened.  When the
    tenant's container is already running, the migrations are left to the
    container: it ran them when it started, and plugin code running in it
    could swap the file while a host-side process has it open.
    """
    db_path = _instance_db_path(data_dir)
    if not os.path.lexists(db_path):
        return True
    try:
        guard_tenant_path(data_dir, db_path)
    except (OSError, ValueError):
        logger.error(
            "Not preparing %s: it is not a regular file inside the instance "
            "directory", db_path,
        )
        return False

    if _tenant_container_running(data_dir):
        migrated = True
    else:
        migrated = _run_instance_db_migrations(data_dir, db_path)

    # Safety defaults are best-effort: don't block startup if they fail.
    try:
        _apply_hosted_db_safety_defaults(db_path)
    except Exception:
        logger.warning(
            "Non-fatal: hosted DB safety defaults failed for %s",
            db_path,
            exc_info=True,
        )
    return migrated


def _seed_instance_db(
    db_path, username, password, force_password_change=False, global_tour_enabled=False
):
    """Pre-seed an instance database with an admin user and complete setup.

    Uses a subprocess to import BananaWiki's own modules so that the full
    schema is initialised correctly.  Falls back to raw SQL if the
    subprocess approach fails.

    When *force_password_change* is ``True`` the admin user will be
    required to set a new password on first login.

    When *global_tour_enabled* is ``True`` the instance admin will be
    marked as requiring onboarding, which shows the setup wizard and
    guided tour on first login.
    """
    hashed_pw = generate_password_hash(password)

    # Build the setup script lines.
    script_lines = [
        "import sys, os",
        "sys.path.insert(0, os.environ['_BW_ROOT'])",
        "os.environ['BW_DATABASE_PATH'] = sys.argv[1]",
        "os.environ['BW_INSTANCE_DIR'] = os.path.dirname(sys.argv[1])",
        "os.environ['BW_UPLOAD_FOLDER'] = os.path.join(os.path.dirname(sys.argv[1]), 'uploads')",
        "os.environ['BW_ATTACHMENT_FOLDER'] = os.path.join(os.path.dirname(sys.argv[1]), 'attachments')",
        "os.environ['BW_CHAT_ATTACHMENT_FOLDER'] = os.path.join(os.path.dirname(sys.argv[1]), 'chat_attachments')",
        "os.environ['BW_KANBAN_ATTACHMENT_FOLDER'] = os.path.join(os.path.dirname(sys.argv[1]), 'kanban_attachments')",
        "os.environ['BW_CUSTOM_PAGE_FILES_FOLDER'] = os.path.join(os.path.dirname(sys.argv[1]), 'custom_page_files')",
        "os.environ['BW_LOG_FILE'] = os.path.join(os.path.dirname(sys.argv[1]), 'bananawiki.log')",
        "import db",
        "db.init_db()",
        "uid = db.create_user(sys.argv[2], sys.argv[3], 'admin')",
    ]
    if force_password_change:
        script_lines.append("db.update_user(uid, force_password_change=1)")
    if global_tour_enabled:
        script_lines.append("db.update_user(uid, onboarding_required=1)")
    script_lines.append("db.update_site_settings(setup_done=1)")
    setup_script = "\n".join(script_lines)

    env = os.environ.copy()
    env["_BW_ROOT"] = config.BW_ROOT

    try:
        result = subprocess.run(
            [sys.executable, "-c", setup_script, db_path, username, hashed_pw],
            cwd=config.BW_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "Subprocess seeding failed (rc=%d): %s",
            result.returncode,
            result.stderr[:500],
        )
    except Exception:
        logger.exception("Subprocess seeding failed")

    # Fallback: seed directly with raw SQL.
    return _seed_instance_db_raw(
        db_path,
        username,
        hashed_pw,
        force_password_change=force_password_change,
        global_tour_enabled=global_tour_enabled,
    )


def _seed_instance_db_raw(
    db_path, username, hashed_pw, force_password_change=False, global_tour_enabled=False
):
    """Fallback: seed the instance database with raw SQL.

    Creates the minimum tables needed and inserts the admin user.
    The BananaWiki process will handle full schema migrations on startup.

    When *force_password_change* is ``True`` the admin user will be
    required to set a new password on first login.

    When *global_tour_enabled* is ``True`` the instance admin will be
    marked as requiring onboarding, which shows the setup wizard and
    guided tour on first login.
    """
    try:
        user_id = "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        conn = sqlite3.connect(db_path, timeout=20)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=20000")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS site_settings (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    site_name   TEXT NOT NULL DEFAULT 'BananaWiki',
                    setup_done  INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO site_settings (id, setup_done) VALUES (1, 1)"
            )
            conn.execute("UPDATE site_settings SET setup_done = 1 WHERE id = 1")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id       TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password TEXT NOT NULL,
                    role     TEXT NOT NULL DEFAULT 'user'
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, password, role) "
                "VALUES (?, ?, ?, 'admin')",
                (user_id, username, hashed_pw),
            )

            # Ensure the force_password_change column exists and set it if needed.
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN force_password_change INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            if force_password_change:
                conn.execute("UPDATE users SET force_password_change = 1")
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN onboarding_required INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            if global_tour_enabled:
                conn.execute("UPDATE users SET onboarding_required = 1")

            conn.commit()
        finally:
            conn.close()

        # Ensure the .secret_key file exists so all Gunicorn workers share
        # the same Flask secret key.  Without this, a TOCTOU race between
        # workers calling _load_secret_key() can produce different keys,
        # which makes session cookies unreadable across workers and causes
        # CSRF errors ("Your session has expired").
        data_dir = os.path.dirname(os.path.abspath(db_path))
        secret_path = os.path.join(data_dir, ".secret_key")
        if not os.path.exists(secret_path):
            key = secrets.token_hex(32)
            # Atomic write: temp file + rename prevents partial reads.
            fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".secret_key_")
            try:
                os.write(fd, key.encode("utf-8"))
                os.close(fd)
                if os.name != "nt":
                    os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, secret_path)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        return True
    except Exception:
        logger.exception("Raw SQL seeding failed for %s", db_path)
        return False


def _reconstruct_wiki_db_from_json(target_db_path, json_payload):
    """Build a fresh BananaWiki SQLite DB at *target_db_path* from a
    ``site_export.json`` payload.

    Used by ``provision_instance_from_archive`` to accept legacy
    standalone-wiki archives that ship without a raw ``bananawiki.db``.

    Implementation note: we temporarily point the wiki's
    ``config.DATABASE_PATH`` at *target_db_path* so :func:`db.init_db`
    creates the schema in the right place, then call
    :func:`db.import_site_data` against the same path.  This is safe in
    the hosting process because nothing else in ``hosting/`` imports
    the standalone wiki's ``db`` module (verified at write time).
    """
    import config as _wiki_config  # noqa: WPS433 (deliberate runtime import)
    from db import init_db, import_site_data as _db_import_site_data  # noqa: WPS433

    original_db_path = _wiki_config.DATABASE_PATH
    try:
        _wiki_config.DATABASE_PATH = target_db_path
        # Ensure the parent dir exists before init_db opens a connection.
        os.makedirs(os.path.dirname(target_db_path), exist_ok=True)
        init_db()
        _db_import_site_data(json_payload, "delete_all")
    finally:
        _wiki_config.DATABASE_PATH = original_db_path


def _attribute_wiki_db_pages_to_system(db_path):
    """Assign imported page and page-history authors in *db_path* to the system."""
    system_id = "-1"
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        page_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(pages)").fetchall()
        }
        if "last_edited_by" in page_cols:
            conn.execute("UPDATE pages SET last_edited_by=?", (system_id,))
        history_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(page_history)").fetchall()
        }
        if "edited_by" in history_cols:
            conn.execute("UPDATE page_history SET edited_by=?", (system_id,))
        conn.commit()
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        finally:
            conn.close()
