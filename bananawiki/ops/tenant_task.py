"""Database work on a hosted wiki, run *inside* its tenant container.

The hosting portal never opens a tenant's SQLite database on the host: the
file is written by the tenant (and by the plugins it runs), so parsing it or
running migrations against it host-side would hand the tenant a way into the
portal (1.4 audit). Instead the runtime agent runs this module in the tenant's
own sandbox, with ``docker exec -i`` while the container runs or in a one-shot
container (same image, user, mount and hardening, no network) while it is
stopped::

    python -m bananawiki.ops.tenant_task   < {"action": "list_users", ...}

One JSON request is read from stdin (passwords travel there, never on argv)
and one JSON line is written to stdout: ``{"ok": true, ...}`` or
``{"ok": false, "error": <code>, "detail": <text>}``. External plugins are
never loaded here, so a hostile plugin cannot intercept a password reset.

Files exchanged with the host (database snapshots) go through the private
``/data/.bw-host`` folder under names the host chooses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

EXCHANGE = ".bw-host"
EXCHANGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\.db")
USERNAME = re.compile(r"[A-Za-z0-9_-]{3,50}")
ROLES = ("user", "editor", "admin", "owner")
VOLATILE_TABLES = ("user_sessions", "rate_limit_hits", "tts_generations")
MAX_REQUEST = 64 * 1024
MAX_FIELD = 200
PLUGIN_MANIFEST_MAX = 64 * 1024
_ID_ALPHABET = string.ascii_lowercase + string.digits


class TaskError(Exception):
    """A refused request; ``code`` is one of the runtime failure codes."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# ── Plumbing ──────────────────────────────────────────────────────────────────


def _config():
    from bananawiki.wiki.config import load_config

    return load_config()


def _database(cfg: Any):
    from bananawiki.wiki.app import open_database

    return open_database(cfg)


def _db_path(cfg: Any) -> Path:
    return Path(cfg.database_path)


def _require_database(cfg: Any) -> None:
    path = _db_path(cfg)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TaskError("db_unsafe", "the database is not a regular file")
    if not path.is_file() or path.stat().st_size == 0:
        raise TaskError("db_missing", "the wiki has no database yet")


@contextmanager
def _session(cfg: Any):
    """An upgraded database and a query session on it."""
    from bananawiki.core.sqlite import Session

    _require_database(cfg)
    database = _database(cfg)
    database.initialize()
    conn = database.connect()
    try:
        yield Session(conn)
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _text(request: dict[str, Any], key: str, *, limit: int = MAX_FIELD, required: bool = True) -> str:
    value = request.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise TaskError("invalid", f"{key} is missing or invalid")
    return value


def _int(request: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = request.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise TaskError("invalid", f"{key} must be between {low} and {high}")
    return value


def _username(request: dict[str, Any], key: str = "username") -> str:
    name = _text(request, key, limit=50).strip()
    if not USERNAME.fullmatch(name):
        raise TaskError("invalid", "user names are 3-50 letters, digits, '_' or '-'")
    return name


def _password(request: dict[str, Any]) -> str:
    from bananawiki.core import passwords

    value = request.get("password")
    if not isinstance(value, str) or passwords.validate_password(value):
        raise TaskError("invalid", "the password does not meet the password policy")
    return value


def _hash(cfg: Any, password: str) -> str:
    from bananawiki.core import passwords

    return passwords.hash_password(password, cfg.password_hash_method)


def _now() -> str:
    from bananawiki.core.timeutil import now_sql

    return now_sql()


def _new_user_id(db: Any) -> str:
    while True:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(8))
        if not db.scalar("SELECT 1 FROM users WHERE id = ?", (candidate,)):
            return candidate


def _revoke(db: Any, user_ids: list[str]) -> None:
    """End the sessions and deactivate the API tokens of *user_ids*."""
    from bananawiki.core.sqlite import table_exists

    for user_id in user_ids:
        db.execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                   (_now(), user_id))
        if table_exists(db.conn, "api_service__tokens"):
            db.execute("UPDATE api_service__tokens SET active = 0 WHERE user_id = ? AND active = 1", (user_id,))


def _exchange_file(cfg: Any, request: dict[str, Any]) -> Path:
    name = _text(request, "name", limit=70)
    if not EXCHANGE_NAME.fullmatch(name):
        raise TaskError("invalid", "invalid exchange file name")
    folder = Path(cfg.instance_dir) / EXCHANGE
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise TaskError("db_unsafe", "the exchange folder is not a directory")
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Policy written into the wiki database ─────────────────────────────────────


def _apply_policy(db: Any, request: dict[str, Any]) -> None:
    """Upload limits and the platform's blocked extensions (1.4 ``apply_upload_policy``)."""
    size_mb = _int(request, "upload_max_mb", 100, 1, 2048)
    blocked = request.get("blocked_extensions", [])
    if not isinstance(blocked, list) or len(blocked) > 500 or not all(
        isinstance(item, str) and item.isalnum() and len(item) <= 20 for item in blocked
    ):
        raise TaskError("invalid", "blocked_extensions must be a list of extensions")
    db.execute("INSERT OR IGNORE INTO site_settings (id) VALUES (1)")
    db.execute("UPDATE site_settings SET upload_max_size_mb = ?, platform_upload_blacklist = ? WHERE id = 1",
               (size_mb, ", ".join(sorted({item.lower() for item in blocked}))))
    db.execute("UPDATE site_settings SET upload_mode = 'allow_all' WHERE id = 1 AND upload_mode = 'whitelist' "
               "AND COALESCE(upload_whitelist, '') = ''")
    # The shared GPU server reaches a hosted wiki only through its environment
    # (and only when the TTS policy allows it); never keep it in the database.
    db.execute("UPDATE site_settings SET tts_gpu_enabled = 0, tts_gpu_url = '', tts_gpu_auth_token = '' "
               "WHERE id = 1 AND (tts_gpu_url != '' OR tts_gpu_auth_token != '')")


# ── Actions ───────────────────────────────────────────────────────────────────


def seed(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Create the database of a new (or factory-reset) wiki with its first administrator."""
    path = _db_path(cfg)
    if path.exists() and path.stat().st_size:
        raise TaskError("data_exists", "the wiki already has a database")
    username = _username(request)
    password = _password(request)
    database = _database(cfg)
    database.initialize()
    from bananawiki.core.sqlite import Session

    conn = database.connect()
    try:
        db = Session(conn)
        with db.transaction():
            db.insert("users", {
                "id": _new_user_id(db), "username": username, "password": _hash(cfg, password), "role": "owner",
                "force_password_change": 1 if request.get("force_password_change") else 0,
                "onboarding_required": 1 if request.get("onboarding") else 0, "created_at": _now(),
            })
            db.execute("UPDATE site_settings SET setup_done = 1 WHERE id = 1")
            _apply_policy(db, request)
    finally:
        conn.close()
    return {"username": username}


def migrate(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Validate and upgrade a database the host placed here (import, copy, restore).

    An import may bring only 1.4's ``site_export.json``; the host leaves it in
    the exchange folder and it is turned into a database first. Imported
    databases must carry BananaWiki's application id, pass an integrity check
    and hold a finished setup, an administrator and a home page.
    """
    from bananawiki.core.sqlite import DatabaseUnavailable, dict_row
    from bananawiki.wiki.features.site_admin import migration

    path = _db_path(cfg)
    dump = Path(cfg.instance_dir) / EXCHANGE / "site_export.json"
    try:
        if not path.exists() and dump.is_file() and not dump.is_symlink():
            if dump.stat().st_size > migration.MAX_JSON_BYTES:
                raise TaskError("too_large", "site_export.json is too large")
            migration._database_from_json(dump.read_bytes(), path)
        _require_database(cfg)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = dict_row
        try:
            migration._check_identity(conn)
        finally:
            conn.close()
        with _session(cfg) as db:
            if request.get("imported"):
                migration._check_content(db.conn)
            for table in VOLATILE_TABLES:
                db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            if request.get("revoke_credentials"):
                _revoke(db, db.column("SELECT id FROM users"))
            if isinstance(request.get("policy"), dict):
                _apply_policy(db, request["policy"])
    except migration.MigrationError as error:
        raise TaskError("archive_invalid", error.key) from None
    except (DatabaseUnavailable, sqlite3.DatabaseError) as error:
        raise TaskError("archive_invalid", str(error)[:300]) from None
    finally:
        if dump.is_file() and not dump.is_symlink():
            dump.unlink()
    return {}


def list_users(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    limit = _int(request, "limit", 200, 1, 1000)
    offset = _int(request, "offset", 0, 0, 10_000_000)
    with _session(cfg) as db:
        total = int(db.scalar("SELECT COUNT(*) FROM users", default=0))
        rows = db.all("SELECT username, role, created_at FROM users ORDER BY created_at, rowid LIMIT ? OFFSET ?",
                      (limit, offset))
    return {"users": [{key: str(row[key] or "")[:MAX_FIELD] for key in ("username", "role", "created_at")}
                      for row in rows], "total": total}


def set_password(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Create the user, or set its password and role and end its sessions and tokens."""
    username = _username(request)
    password = _password(request)
    role = request.get("role", "user")
    if role not in ROLES:
        raise TaskError("invalid", "unknown role")
    hashed = _hash(cfg, password)
    with _session(cfg) as db, db.transaction():
        user = db.one("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,))
        if user is None:
            db.insert("users", {"id": _new_user_id(db), "username": username, "password": hashed, "role": role,
                                "created_at": _now()})
            return {"created": True}
        db.execute("UPDATE users SET password = ?, role = ? WHERE id = ?", (hashed, role, user["id"]))
        _revoke(db, [user["id"]])
    return {"created": False}


def remove_user(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    username = _username(request)
    with _session(cfg) as db, db.transaction():
        user = db.one("SELECT id, role FROM users WHERE username = ? COLLATE NOCASE", (username,))
        if user is None:
            raise TaskError("user_not_found", username)
        if user["role"] == "owner":
            raise TaskError("protected_user", username)
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    return {}


def reset_admin(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """New password (to be changed at next sign-in) for the named admin, else for every admin and owner."""
    username = _username(request)
    hashed = _hash(cfg, _password(request))
    with _session(cfg) as db, db.transaction():
        named = db.one("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,))
        if named is not None:
            targets = [named]
        else:
            targets = db.all("SELECT id, username FROM users WHERE role IN ('admin', 'owner') "
                             "ORDER BY created_at, rowid")
        if not targets:
            user_id = _new_user_id(db)
            db.insert("users", {"id": user_id, "username": username, "password": hashed, "role": "owner",
                                "force_password_change": 1, "created_at": _now()})
            return {"username": username}
        for user in targets:
            db.execute("UPDATE users SET password = ?, force_password_change = 1 WHERE id = ?", (hashed, user["id"]))
        _revoke(db, [user["id"] for user in targets])
    return {"username": targets[0]["username"]}


def analytics(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    from bananawiki.core.timeutil import utcnow

    days = _int(request, "days", 30, 1, 365)
    kinds = ("request", "page_view", "error")
    end = utcnow().date()
    start = end - timedelta(days=days - 1)
    index = {(start + timedelta(days=offset)).isoformat(): dict.fromkeys(kinds, 0) for offset in range(days)}
    with _session(cfg) as db:
        rows = db.all("SELECT day, kind, count FROM analytics_daily WHERE day BETWEEN ? AND ?",
                      (start.isoformat(), end.isoformat()))
    for row in rows:
        if row["day"] in index and row["kind"] in kinds:
            index[row["day"]][row["kind"]] = int(row["count"] or 0)
    daily = [{"day": day, **counts} for day, counts in sorted(index.items())]
    return {"window_days": days, "totals": {kind: sum(entry[kind] for entry in daily) for kind in kinds},
            "daily": daily}


def apply_policy(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    with _session(cfg) as db, db.transaction():
        _apply_policy(db, request)
    return {}


def _external_plugin_ids(cfg: Any) -> list[str]:
    """Plugin ids whose code sits in the external plugin folder (folder names and manifest ids)."""
    root = Path(cfg.folders.plugins)
    if root.is_symlink() or not root.is_dir():
        return []
    ids: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        ids.append(entry.name)
        manifest = entry / "plugin.json"
        try:
            if manifest.is_symlink() or manifest.stat().st_size > PLUGIN_MANIFEST_MAX:
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
            ids.append(data["id"][:MAX_FIELD])
    return list(dict.fromkeys(ids))


def quarantine(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Disable every external plugin row: ids found in the plugin folder and every ``builtin=0`` row.

    A plugin that has run can set ``builtin=1`` on its own row, so the folder
    decides, not that column.
    """
    ids = _external_plugin_ids(cfg)
    with _session(cfg) as db, db.transaction():
        db.execute("CREATE TEMP TABLE quarantine_ids (id TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO quarantine_ids (id) VALUES (?)", [(item,) for item in ids])
        disabled = db.execute("UPDATE plugins SET enabled = 0, disabled_at = ? WHERE enabled = 1 AND "
                              "(builtin = 0 OR id IN (SELECT id FROM quarantine_ids))", (_now(),)).rowcount
        db.execute("DROP TABLE quarantine_ids")
    return {"disabled": disabled}


def snapshot(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """A consistent, checked copy of the database in the exchange folder.

    With ``portable``, login sessions and caches are removed from the copy
    (archives that leave the platform).
    """
    target = _exchange_file(cfg, request)
    if target.exists() or target.is_symlink():
        raise TaskError("data_exists", "the snapshot name is taken")
    with _session(cfg) as db:
        pending = target.with_name(target.name + ".part")
        pending.unlink(missing_ok=True)
        copy = sqlite3.connect(str(pending))
        try:
            db.conn.backup(copy, pages=512)
            copy.execute("PRAGMA journal_mode=DELETE")
            if request.get("portable"):
                for table in VOLATILE_TABLES:
                    copy.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
                copy.commit()
                copy.execute("VACUUM")
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise TaskError("failed", "the snapshot failed its integrity check")
        finally:
            copy.close()
    os.chmod(pending, 0o600)
    os.replace(pending, target)
    return {"file": f"{EXCHANGE}/{target.name}", "size": target.stat().st_size, "sha256": _sha256(target)}


def restore_db(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Replace the database with a snapshot the host placed in the exchange folder."""
    from bananawiki.wiki.migrations import APPLICATION_ID

    source_path = _exchange_file(cfg, request)
    if source_path.is_symlink() or not source_path.is_file():
        raise TaskError("not_found", "the snapshot was not delivered")
    database = _database(cfg)
    try:
        source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        try:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok" or \
                    source.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise TaskError("db_unsafe", "the snapshot is not an intact BananaWiki database")
            target = database.connect()
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.DatabaseError as error:
        raise TaskError("db_unsafe", str(error)[:300]) from None
    finally:
        source_path.unlink(missing_ok=True)
    database.initialize()
    return {}


def discard(cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Remove the exchange folder (host-side cleanup after a failed transfer)."""
    folder = Path(cfg.instance_dir) / EXCHANGE
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir():
        shutil.rmtree(folder)
    return {}


ACTIONS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "seed": seed, "migrate": migrate, "list_users": list_users, "set_password": set_password,
    "remove_user": remove_user, "reset_admin": reset_admin, "analytics": analytics, "apply_policy": apply_policy,
    "quarantine": quarantine, "snapshot": snapshot, "restore_db": restore_db, "discard": discard,
}


def run(raw: str) -> dict[str, Any]:
    """Handle one request; always returns a response object."""
    try:
        if len(raw) > MAX_REQUEST:
            raise TaskError("too_large", "request too large")
        try:
            request = json.loads(raw)
        except ValueError:
            raise TaskError("invalid", "requests are one JSON object") from None
        if not isinstance(request, dict) or request.get("action") not in ACTIONS:
            raise TaskError("invalid", "unknown action")
        return {"ok": True, **ACTIONS[request["action"]](_config(), request)}
    except TaskError as error:
        return {"ok": False, "error": error.code, "detail": error.detail[:300]}
    except Exception as error:  # noqa: BLE001 - report every failure as one JSON line
        return {"ok": False, "error": "failed", "detail": type(error).__name__}


def main() -> int:
    response = run(sys.stdin.read(MAX_REQUEST + 1))
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
