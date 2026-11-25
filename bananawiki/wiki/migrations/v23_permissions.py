"""Schema version 23: one permission model for pages, Kanban boards and canvases.

Who may do what is decided by the permission catalogue alone (base roles,
custom roles, individual permissions); the per-feature role thresholds and
switches are retired. This version translates the old settings so that
existing wikis keep their behaviour, except for the new defaults of the base
roles (members create pages, boards and canvases and work on their own).

Schema
------
* ``base_role_permissions``: the wiki's changes to the built-in defaults of
  the ``user`` and ``editor`` roles (``granted`` 1 adds a key, 0 removes it).
* ``pages.created_by``: who created a page (for "own" pages). Existing pages
  keep it empty: nobody gains rights over pages created before.
* ``kanban_boards.visibility`` and ``canvas__layouts.visibility`` take
  ``private`` (only the people and roles it is shared with), ``members``
  (every member who may view boards or canvases) or ``public`` (also visitors
  without an account while the wiki is public); new items are private.
* Shares of both features grant ``view`` or ``edit`` (Kanban's ``write`` is
  renamed; canvas's ``none`` refusals are gone).

Settings translated (each is kept in ``site_settings`` and no longer read)
------------------------------------------------------------------------
The roles a setting reached: ``all`` = user and editor, ``editor`` = editor,
``admin`` = none of the base roles. An "open access" switch reached all.

* ``kanban_write_access`` / ``kanban_open_access`` -> ``kanban.edit_all`` for
  the roles reached (base-role defaults, and every custom role or individual
  permission set of those roles that holds ``kanban.view``); removed from the
  others.
* ``kanban_access`` / ``kanban_open_access`` (who saw ``public`` boards and
  boards shared with a role) and ``kanban_public_access_enabled`` -> each
  board's visibility, see :func:`_new_visibility`; role shares for roles the
  setting did not reach had no effect and are deleted, as are shares with
  the admin role (administrators see every board).
* ``kanban.create`` in a custom role or individual permission set stays only
  where it allowed creating (the role reached ``kanban_write_access``).
* ``canvas_open_access`` -> ``canvas.edit_all`` for both roles (removed
  otherwise), and every canvas that was neither public nor archived becomes
  ``members``: open access let every member open every such canvas.
* ``canvas_access`` / ``canvas_public_access_enabled`` -> visibility, as for
  boards. ``canvas_write_access`` only limited creating, which members now
  may do by default; custom roles and individual sets keep ``canvas.create``
  as chosen (an explicit grant allowed creating below the setting before).
* Canvas ``none`` refusals: where dropping one would let someone see or edit
  a canvas they could not before, the canvas becomes ``private`` and is
  shared explicitly with exactly the accounts that had access, at their
  level. Refusals that changed nothing are just deleted.
* ``page_builder_access`` -> ``page.use_builder`` for the roles reached.
* The chat switches (administrators were never bound by them) ->
  permissions taken from members and editors (base-role defaults, custom
  roles, individual sets) where a switch was off: ``chat_dm_enabled`` ->
  ``chat.dm`` and ``chat.start_dm``; ``chat_allow_dm_creation`` -> the new
  ``chat.start_dm`` (given to every set holding ``chat.dm`` otherwise);
  ``chat_group_enabled`` -> ``chat.group`` and ``chat.create_group``;
  ``chat_allow_group_creation`` -> ``chat.create_group``;
  ``chat_attachments_enabled`` -> ``chat.upload``.
* ``page.delete`` is renamed ``page.delete_all``.
* Custom roles and individual permission sets keep what they allowed: they
  get ``kanban.*`` / ``canvas.*`` ``edit_own``, ``delete_own`` and
  ``share_own`` when they could open boards or canvases (creators always
  managed their items), and nothing else new. The new member defaults
  (creating and editing one's own pages, drafts, uploads) apply to accounts
  on the base role's defaults; administrators tick them in a custom role or
  individual set where wanted.
* Editors whose reading was limited to some categories while their writing
  was not read every category before (writing implied reading); their read
  limit is lifted, since unrestricted writing now means "wherever one may read".

Another double switch
---------------------
* ``api_service_enabled`` (the REST API's own on/off, inside its plugin
  switch) is retired: where it was off, the ``api_service`` plugin is
  switched off, so the API stays off.

The statements are frozen here: later changes belong in later versions.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

from ...core.sqlite import column_names, quote_identifier, table_exists, tuples
from ...core.timeutil import is_future

BASE = ("user", "editor")
REACHED = {"all": frozenset(BASE), "editor": frozenset({"editor"}), "admin": frozenset()}
_DEFAULTS_AT_VERSION_22 = {
    "user": frozenset((
        "assessment.view", "attachment.view", "canvas.view", "category.view_all", "chat.create_group", "chat.dm",
        "chat.group", "chat.upload", "contribution.propose", "history.view", "kanban.view", "page.view_all",
        "profile.edit_own", "profile.view", "search.pages", "search.users",
    )),
    "editor": frozenset((
        "assessment.manage", "assessment.view", "attachment.delete_own", "attachment.upload", "attachment.view",
        "canvas.create", "canvas.view", "category.create", "category.delete", "category.edit",
        "category.manage_sequential", "category.reorder", "category.view_all", "chat.create_group", "chat.dm",
        "chat.group", "chat.upload", "draft.create", "draft.delete_own", "draft.view_own", "history.view",
        "kanban.create", "kanban.view", "page.create", "page.edit_all", "page.edit_metadata", "page.view_all",
        "page.view_deindexed", "profile.edit_own", "profile.view", "search.pages", "search.users",
        "tag.edit_custom", "tag.edit_difficulty",
    )),
}
#: Built-in defaults of version 23 for the keys this version decides per wiki.
_BUILTIN_AT_VERSION_23 = {
    ("editor", "kanban.edit_all"): True, ("user", "kanban.edit_all"): False,
    ("editor", "canvas.edit_all"): True, ("user", "canvas.edit_all"): False,
    ("editor", "page.use_builder"): False, ("user", "page.use_builder"): False,
}
_OWN = ("edit_own", "delete_own", "share_own")
_ITEM_VERBS = ("create", "edit_own", "delete_own", "share_own", "edit_all", "delete_all")
_VISIBILITY_CHECK = re.compile(r"CHECK\s*\(\s*\"?visibility\"?\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE)
_LEVEL_CHECK = {
    "kanban_board_shares": re.compile(r"CHECK\s*\(\s*\"?access_level\"?\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE),
    "canvas__permissions": re.compile(r"CHECK\s*\(\s*\"?permission\"?\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE),
}


def upgrade(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "site_settings"):
        return
    old = _old_settings(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS base_role_permissions ("
        "role TEXT NOT NULL CHECK (role IN ('user', 'editor')), permission_key TEXT NOT NULL, "
        "granted INTEGER NOT NULL CHECK (granted IN (0, 1)), PRIMARY KEY (role, permission_key)) WITHOUT ROWID"
    )
    if table_exists(conn, "pages") and "created_by" not in column_names(conn, "pages"):
        conn.execute("ALTER TABLE pages ADD COLUMN created_by TEXT REFERENCES users(id) ON DELETE SET NULL")
    _translate_permissions(conn, old)
    _lift_read_limits_of_unlimited_writers(conn)
    if table_exists(conn, "kanban_boards"):
        _migrate_boards(conn, old)
    if table_exists(conn, "canvas__layouts"):
        _migrate_canvases(conn, old)
    _one_api_switch(conn)


def _one_api_switch(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "plugins") or "api_service_enabled" not in column_names(conn, "site_settings"):
        return
    if not tuples(conn, "SELECT 1 FROM site_settings WHERE id = 1 AND api_service_enabled = 1"):
        conn.execute("UPDATE plugins SET enabled = 0, disabled_at = datetime('now') "
                     "WHERE id = 'api_service' AND enabled = 1")


# Settings


def _old_settings(conn: sqlite3.Connection) -> dict[str, object]:
    columns = column_names(conn, "site_settings")
    wanted = ("kanban_access", "kanban_write_access", "kanban_open_access", "kanban_public_access_enabled",
              "canvas_access", "canvas_write_access", "canvas_open_access", "canvas_public_access_enabled",
              "page_builder_access", "public_mode", "public_mode_until", "chat_dm_enabled", "chat_group_enabled",
              "chat_allow_dm_creation", "chat_allow_group_creation", "chat_attachments_enabled")
    present = [name for name in wanted if name in columns]
    row = tuples(conn, f"SELECT {', '.join(present)} FROM site_settings WHERE id = 1") if present else []
    values = dict(zip(present, row[0], strict=True)) if row else {}

    def reached(name: str) -> frozenset[str]:
        return REACHED.get(str(values.get(name) or "admin"), frozenset())

    kanban_open = bool(values.get("kanban_open_access"))
    canvas_open = bool(values.get("canvas_open_access"))
    until = values.get("public_mode_until")
    builder = str(values.get("page_builder_access") or "admin")

    def on(name: str) -> bool:
        return values.get(name) is None or bool(values.get(name))

    chat_off = set()
    if not on("chat_dm_enabled"):
        chat_off |= {"chat.dm", "chat.start_dm"}
    if not on("chat_allow_dm_creation"):
        chat_off.add("chat.start_dm")
    if not on("chat_group_enabled"):
        chat_off |= {"chat.group", "chat.create_group"}
    if not on("chat_allow_group_creation"):
        chat_off.add("chat.create_group")
    if not on("chat_attachments_enabled"):
        chat_off.add("chat.upload")
    return {
        "chat_off": frozenset(chat_off),
        "kanban_view": frozenset(BASE) if kanban_open else reached("kanban_access"),
        "kanban_write": frozenset(BASE) if kanban_open else reached("kanban_write_access"),
        "kanban_public": bool(values.get("kanban_public_access_enabled")),
        "canvas_view": frozenset(BASE) if canvas_open else reached("canvas_access"),
        "canvas_open": canvas_open,
        "canvas_public": bool(values.get("canvas_public_access_enabled")),
        "builder": REACHED["all"] if builder == "user" else REACHED.get(builder, frozenset()),
        "public_mode_on": bool(values.get("public_mode")) and (not until or is_future(str(until))),
    }


# Permission sets


def _key_sets(conn: sqlite3.Connection) -> Iterable[tuple[str, object, str, set[str]]]:
    """Every stored permission set: ``(kind, id, base role, keys)``."""
    if table_exists(conn, "custom_roles"):
        for role_id, base in tuples(conn, "SELECT id, base_role FROM custom_roles"):
            keys = {k for (k,) in tuples(conn, "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?",
                                          (role_id,))}
            yield "role", role_id, base, keys
    if table_exists(conn, "user_category_access"):
        for user_id, role in tuples(conn, "SELECT u.id, u.role FROM users u WHERE u.role IN ('user', 'editor') "
                                          "AND EXISTS (SELECT 1 FROM user_category_access a WHERE a.user_id = u.id)"):
            keys = {k for (k,) in tuples(conn, "SELECT permission_key FROM user_permissions WHERE user_id = ?",
                                          (user_id,))}
            yield "user", user_id, role, keys


def _store(conn: sqlite3.Connection, kind: str, owner: object, keys: set[str]) -> None:
    table, column = ("custom_role_permissions", "role_id") if kind == "role" else ("user_permissions", "user_id")
    conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (owner,))
    conn.executemany(f"INSERT INTO {table} ({column}, permission_key) VALUES (?, ?)",
                     [(owner, key) for key in sorted(keys)])


def _translated(base: str, keys: set[str], old: dict[str, object]) -> set[str]:
    keys = set(keys)
    if "page.delete" in keys:
        keys.discard("page.delete")
        keys.add("page.delete_all")
    # Kanban: creating needed the write setting; creators managed their boards.
    if "kanban.view" in keys:
        if "kanban.create" in keys and base not in old["kanban_write"]:  # type: ignore[operator]
            keys.discard("kanban.create")
        if base in old["kanban_write"]:  # type: ignore[operator]
            keys.add("kanban.edit_all")
        keys.update(f"kanban.{verb}" for verb in _OWN)
    if "canvas.view" in keys:
        if old["canvas_open"]:
            keys.add("canvas.edit_all")
        keys.update(f"canvas.{verb}" for verb in _OWN)
    for prefix in ("kanban", "canvas"):  # nothing worked without the view key: keep it that way
        if f"{prefix}.view" not in keys:
            keys.difference_update(f"{prefix}.{verb}" for verb in _ITEM_VERBS)
    if base in old["builder"]:  # type: ignore[operator]
        keys.add("page.use_builder")
    if "chat.dm" in keys:
        keys.add("chat.start_dm")
    if "chat.group" not in keys:
        keys.discard("chat.create_group")  # it needed taking part in groups
    keys.difference_update(old["chat_off"])  # type: ignore[arg-type]
    return keys


def _translate_permissions(conn: sqlite3.Connection, old: dict[str, object]) -> None:
    for kind, owner, base, keys in list(_key_sets(conn)):
        _store(conn, kind, owner, _translated(base, keys, old))
    granted = {
        "kanban.edit_all": old["kanban_write"],
        "canvas.edit_all": frozenset(BASE) if old["canvas_open"] else frozenset(),
        "page.use_builder": old["builder"],
    }
    for key, roles in granted.items():
        for role in BASE:
            wanted = role in roles  # type: ignore[operator]
            if wanted != _BUILTIN_AT_VERSION_23[(role, key)]:
                conn.execute("INSERT OR REPLACE INTO base_role_permissions (role, permission_key, granted) "
                             "VALUES (?, ?, ?)", (role, key, int(wanted)))
    for key in sorted(old["chat_off"]):  # type: ignore[call-overload]
        for role in BASE:  # every chat key is in both roles' built-in defaults
            conn.execute("INSERT OR REPLACE INTO base_role_permissions (role, permission_key, granted) "
                         "VALUES (?, ?, 0)", (role, key))


def _lift_read_limits_of_unlimited_writers(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "user_category_access"):
        users = [user_id for (user_id,) in tuples(
            conn,
            "SELECT u.id FROM users u JOIN user_category_access r ON r.user_id = u.id AND r.access_type = 'read' "
            "AND r.restricted = 1 WHERE u.role = 'editor' AND u.custom_role_id IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM user_category_access w WHERE w.user_id = u.id AND w.access_type = 'write' "
            "AND w.restricted = 1)",
        )]
        for user_id in users:
            conn.execute("UPDATE user_category_access SET restricted = 0 WHERE user_id = ? AND access_type = 'read'",
                         (user_id,))
            conn.execute("DELETE FROM user_allowed_categories WHERE user_id = ? AND access_type = 'read'", (user_id,))
    if table_exists(conn, "custom_roles"):
        roles = [role_id for (role_id,) in tuples(
            conn, "SELECT id FROM custom_roles WHERE base_role = 'editor' AND read_restricted = 1 "
                  "AND write_restricted = 0")]
        for role_id in roles:
            conn.execute("UPDATE custom_roles SET read_restricted = 0 WHERE id = ?", (role_id,))
            conn.execute("DELETE FROM custom_role_categories WHERE role_id = ? AND access_type = 'read'", (role_id,))


# Tables whose CHECK constraints change


def _rebuild(conn: sqlite3.Connection, table: str, pattern: re.Pattern[str], check: str,
             select: dict[str, str], *, default: tuple[str, str] | None = None) -> None:
    """Recreate *table* with *check* in place of the constraint *pattern* matches (SQLite's
    documented procedure), copying the rows through *select* (``{column: expression}``)."""
    sql = tuples(conn, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))[0][0]
    extras = [s for (s,) in tuples(conn, "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN "
                                         "('index', 'trigger') AND sql IS NOT NULL", (table,))]
    new_sql = pattern.sub(check, sql, count=1) if pattern.search(sql) else sql
    if default is not None:
        new_sql = new_sql.replace(*default)
    temporary = f"{table}__v23"
    head = re.compile(rf"^CREATE TABLE\s+(\"?){re.escape(table)}\1", re.IGNORECASE)
    new_sql = head.sub(f"CREATE TABLE {quote_identifier(temporary)}", new_sql, count=1)
    columns = [row[1] for row in tuples(conn, f"PRAGMA table_info({quote_identifier(table)})")]
    names = ", ".join(quote_identifier(c) for c in columns)
    values = ", ".join(select.get(c, quote_identifier(c)) for c in columns)
    conn.execute(new_sql)
    conn.execute(f"INSERT INTO {quote_identifier(temporary)} ({names}) SELECT {values} FROM {quote_identifier(table)}")
    conn.execute(f"DROP TABLE {quote_identifier(table)}")
    conn.execute(f"ALTER TABLE {quote_identifier(temporary)} RENAME TO {quote_identifier(table)}")
    for statement in extras:
        conn.execute(statement)


_VISIBILITY = "CHECK(visibility IN ('private','members','public'))"


def _new_visibility(viewers: frozenset[str], anonymous: bool, public_mode_on: bool) -> tuple[str, frozenset[str]]:
    """The visibility a ``public`` item gets, and the roles to share it with to keep who saw it.

    ``public`` items were seen by the roles in *viewers* and, while public mode
    was on and the feature's switch (*anonymous*) too, by visitors. Others
    saw only what was shared with them, which ``private`` keeps.
    """
    if anonymous and (public_mode_on or viewers == frozenset(BASE)):
        return "public", frozenset()  # anyone could read it already, or will as soon as public mode is on
    if viewers == frozenset(BASE):
        return "members", frozenset()
    return "private", viewers


def _not_in(column: str, values: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    if not values:
        return "1", ()
    return f"{column} NOT IN ({','.join('?' * len(values))})", tuple(sorted(values))


# Kanban


def _migrate_boards(conn: sqlite3.Connection, old: dict[str, object]) -> None:
    viewers: frozenset[str] = old["kanban_view"]  # type: ignore[assignment]
    visibility, share_with = _new_visibility(viewers, bool(old["kanban_public"]), bool(old["public_mode_on"]))
    public_ids = [i for (i,) in tuples(conn, "SELECT id FROM kanban_boards WHERE visibility = 'public'")]
    _rebuild(conn, "kanban_boards", _VISIBILITY_CHECK, _VISIBILITY,
             {"visibility": f"CASE visibility WHEN 'public' THEN '{visibility}' ELSE 'private' END"},
             default=("DEFAULT 'public'", "DEFAULT 'private'"))
    if not table_exists(conn, "kanban_board_shares"):
        return
    # A role share only worked for roles that saw shared boards; administrators see every board.
    condition, params = _not_in("target", viewers)
    conn.execute(f"DELETE FROM kanban_board_shares WHERE share_type = 'role' AND {condition}", params)
    _rebuild(conn, "kanban_board_shares", _LEVEL_CHECK["kanban_board_shares"],
             "CHECK(access_level IN ('view','edit'))",
             {"access_level": "CASE access_level WHEN 'write' THEN 'edit' ELSE 'view' END"})
    for board_id in public_ids:
        for role in sorted(share_with):
            conn.execute("INSERT OR IGNORE INTO kanban_board_shares (board_id, share_type, target, access_level, "
                         "created_at) VALUES (?, 'role', ?, 'view', datetime('now'))", (board_id, role))


# Canvases


_RANK = {None: 0, "view": 1, "edit": 2}


def _holds(conn: sqlite3.Connection, user: tuple, key: str, cache: dict) -> bool:
    """Whether *user* (id, role, custom_role_id) held *key* under the version 22 rules."""
    user_id, role, custom_role_id = user
    if role in ("admin", "owner"):
        return True
    if custom_role_id is not None:
        if custom_role_id not in cache:
            exists = tuples(conn, "SELECT 1 FROM custom_roles WHERE id = ?", (custom_role_id,))
            cache[custom_role_id] = None if not exists else {k for (k,) in tuples(
                conn, "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (custom_role_id,))}
        if cache[custom_role_id] is not None:
            return key in cache[custom_role_id]
    elif tuples(conn, "SELECT 1 FROM user_category_access WHERE user_id = ? LIMIT 1", (user_id,)):
        return bool(tuples(conn, "SELECT 1 FROM user_permissions WHERE user_id = ? AND permission_key = ?",
                           (user_id, key)))
    return key in _DEFAULTS_AT_VERSION_22.get(role, frozenset())


def _canvas_levels(layout: tuple, rows: list[tuple], users: list[tuple], viewers: frozenset[str],
                   open_access: bool, holders: set[str]) -> dict[str, str]:
    """Who reached the canvas under the version 22 rules (creator and administrators left out)."""
    layout_id, creator_id, archived, visibility = layout
    if archived:
        return {}
    by_user = {user_id: level for (user_id, role, level) in rows if user_id}
    by_role = {role: level for (user_id, role, level) in rows if not user_id and role}
    found = {}
    for user_id, role, _custom in users:
        if user_id == creator_id or role in ("admin", "owner") or user_id not in holders:
            continue
        reached = role in viewers
        if reached and open_access:
            level = "edit"
        elif user_id in by_user:
            level = None if by_user[user_id] == "none" else by_user[user_id]
        elif not reached:
            level = None
        elif role in by_role:
            level = None if by_role[role] == "none" else by_role[role]
        else:
            level = "view" if visibility == "public" else None
        if level:
            found[user_id] = level
    return found


def _canvas_levels_after(layout: tuple, rows: list[tuple], users: list[tuple], holders: set[str],
                         edit_all: bool) -> dict[str, str]:
    """Who reaches the canvas under the version 23 rules, refusals dropped."""
    layout_id, creator_id, archived, visibility = layout
    if archived:
        return {}
    by_user = {user_id: level for (user_id, role, level) in rows if user_id and level != "none"}
    by_role = {role: level for (user_id, role, level) in rows if not user_id and role and level != "none"}
    found = {}
    for user_id, role, _custom in users:
        if user_id == creator_id or role in ("admin", "owner") or user_id not in holders:
            continue
        levels = [by_user.get(user_id), by_role.get(role), "view" if visibility in ("members", "public") else None]
        level = max(levels, key=lambda value: _RANK[value])
        if level and edit_all:
            level = "edit"
        if level:
            found[user_id] = level
    return found


def _migrate_canvases(conn: sqlite3.Connection, old: dict[str, object]) -> None:
    viewers: frozenset[str] = old["canvas_view"]  # type: ignore[assignment]
    open_access = bool(old["canvas_open"])
    public, share_with = _new_visibility(viewers, bool(old["canvas_public"]), bool(old["public_mode_on"]))
    shares = table_exists(conn, "canvas__permissions")
    public_ids = [i for (i,) in tuples(conn, "SELECT id FROM canvas__layouts WHERE visibility = 'public'")]
    refused = [i for (i,) in tuples(conn, "SELECT DISTINCT layout_id FROM canvas__permissions "
                                          "WHERE permission = 'none'")] if shares else []
    users: list[tuple] = tuples(conn, "SELECT id, role, custom_role_id FROM users") if refused else []
    cache: dict = {}
    holders = {user[0] for user in users if _holds(conn, user, "canvas.view", cache)}
    before: dict[int, dict[str, str]] = {}
    for layout_id in refused:
        layout = tuples(conn, "SELECT id, creator_id, is_archived, visibility FROM canvas__layouts WHERE id = ?",
                        (layout_id,))
        rows = tuples(conn, "SELECT user_id, role, permission FROM canvas__permissions WHERE layout_id = ?",
                      (layout_id,))
        if layout:
            before[layout_id] = _canvas_levels(layout[0], rows, users, viewers, open_access, holders)
    # Open access let every member open every canvas that was not archived (archived ones stayed their
    # creator's).
    others = "members" if open_access else "private"
    _rebuild(conn, "canvas__layouts", _VISIBILITY_CHECK, _VISIBILITY,
             {"visibility": f"CASE WHEN visibility = 'public' THEN '{public}' WHEN is_archived = 0 THEN '{others}' "
                            "ELSE 'private' END"})
    if not shares:
        return
    # A role row only worked for roles that saw shared canvases; administrators see every canvas.
    condition, params = _not_in("role", viewers)
    conn.execute(f"DELETE FROM canvas__permissions WHERE user_id IS NULL AND {condition}", params)
    for layout_id in public_ids:
        for role in sorted(share_with):
            conn.execute("INSERT OR IGNORE INTO canvas__permissions (layout_id, role, permission, created_at) "
                         "VALUES (?, ?, 'view', datetime('now'))", (layout_id, role))
    for layout_id, had in before.items():
        layout = tuples(conn, "SELECT id, creator_id, is_archived, visibility FROM canvas__layouts WHERE id = ?",
                        (layout_id,))[0]
        rows = tuples(conn, "SELECT user_id, role, permission FROM canvas__permissions WHERE layout_id = ?",
                      (layout_id,))
        after = _canvas_levels_after(layout, rows, users, holders, open_access)
        if any(_RANK[level] > _RANK[had.get(user_id)] for user_id, level in after.items()):
            # Dropping the refusal would let someone in: share with exactly the accounts that had access.
            conn.execute("DELETE FROM canvas__permissions WHERE layout_id = ?", (layout_id,))
            conn.execute("UPDATE canvas__layouts SET visibility = 'private' WHERE id = ?", (layout_id,))
            conn.executemany("INSERT INTO canvas__permissions (layout_id, user_id, permission, created_at) "
                             "VALUES (?, ?, ?, datetime('now'))",
                             [(layout_id, user_id, level) for user_id, level in sorted(had.items())])
    conn.execute("DELETE FROM canvas__permissions WHERE permission = 'none'")
    _rebuild(conn, "canvas__permissions", _LEVEL_CHECK["canvas__permissions"], "CHECK(permission IN ('view','edit'))",
             {})
