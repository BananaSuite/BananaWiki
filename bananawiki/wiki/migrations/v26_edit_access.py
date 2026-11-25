"""Schema version 26: a person's own permissions are changes to their role, and requests for edit access.

A person's own permissions
--------------------------
Before this version, an account with rows in ``user_category_access`` had
its own *complete* permission set in ``user_permissions``: the role's
defaults stopped applying to it altogether (an implicit switch, flipped by
saving the account's permission page once). Now ``user_permissions`` holds
*changes* to what the person's role gives (``granted`` 1 adds a key, 0 takes
it away), on top of their base role's defaults or their custom role.

Nobody's access changes:

* accounts that had their own set get a row for every key of the catalogue
  of version 25: 1 for the keys they held (as the version 25 rules read the
  set: implied keys added, keys their role may not hold left out), 0 for the
  others. They keep exactly the keys they had, also when the role's defaults
  change later, until an administrator saves or resets their permissions;
* rows the version 25 rules never read are deleted: permission rows of
  accounts without category rows (their role's defaults applied), and every
  row of administrators (they hold everything) and of custom role holders
  (the role decided).

Requests for edit access
------------------------
``site_settings.access_requests_enabled`` (off by default) lets members ask
for edit access from a page they can read but not edit; ``access_requests``
keeps the requests: scope ``wiki`` (editing other people's pages) or
``category`` (editing in one category), an optional message, and the
decision. One open request per person and scope.

The statements and the catalogue are frozen here: later changes belong in later versions.
"""

from __future__ import annotations

import sqlite3

from ...core.sqlite import add_columns, column_names, table_exists, tuples

#: The permission catalogue of version 25.
_CATALOGUE_AT_VERSION_25 = (
    "assessment.manage", "assessment.view", "attachment.delete_any", "attachment.delete_own", "attachment.upload",
    "attachment.view", "canvas.create", "canvas.delete_all", "canvas.delete_own", "canvas.edit_all",
    "canvas.edit_own", "canvas.share_own", "canvas.view", "category.create", "category.delete", "category.edit",
    "category.manage_sequential", "category.reorder", "category.view_all", "chat.create_group", "chat.dm",
    "chat.group", "chat.start_dm", "chat.upload", "contribution.propose", "contribution.review",
    "custom_page.manage", "draft.create", "draft.delete_own", "draft.transfer", "draft.view_own", "history.delete",
    "history.revert", "history.transfer", "history.view", "invite.delete", "invite.generate", "invite.view",
    "kanban.create", "kanban.delete_all", "kanban.delete_own", "kanban.edit_all", "kanban.edit_own",
    "kanban.share_own", "kanban.view", "page.create", "page.deindex", "page.delete_all", "page.delete_own",
    "page.edit_all", "page.edit_metadata", "page.edit_own", "page.export_pdf", "page.use_builder", "page.view_all",
    "page.view_deindexed", "profile.edit_own", "profile.view", "search.pages", "search.users", "tag.edit_custom",
    "tag.edit_difficulty",
)
_EDITOR_ONLY_AT_VERSION_25 = frozenset((
    "attachment.delete_any", "category.create", "category.delete", "category.edit", "category.manage_sequential",
    "category.reorder", "contribution.review", "draft.transfer", "history.delete", "history.revert",
    "history.transfer", "invite.delete", "invite.generate", "invite.view", "page.deindex",
))
_ADMIN_ONLY_AT_VERSION_25 = frozenset(("custom_page.manage",))
_IMPLIED_AT_VERSION_25 = {
    **{f"{kind}.{verb}": f"{kind}.view" for kind in ("kanban", "canvas")
       for verb in ("create", "delete_all", "delete_own", "edit_all", "edit_own", "share_own")},
    **{f"category.{verb}": "category.view_all"
       for verb in ("create", "delete", "edit", "manage_sequential", "reorder")},
    "chat.create_group": "chat.group", "chat.start_dm": "chat.dm",
    "contribution.propose": "page.view_all", "contribution.review": "page.view_all",
    "history.delete": "history.view", "history.revert": "history.view",
    **{f"page.{verb}": "page.view_all" for verb in ("create", "deindex", "delete_all", "delete_own", "edit_all",
                                                     "edit_metadata", "edit_own", "use_builder", "view_deindexed")},
}
_RENAMED = {"page.delete": "page.delete_all"}


def _held(role: str, stored: set[str]) -> set[str]:
    """The keys the version 25 rules read from a person's own set (``permissions.sanitize``)."""
    if role == "editor":
        allowed = set(_CATALOGUE_AT_VERSION_25) - _ADMIN_ONLY_AT_VERSION_25
    elif role == "user":
        allowed = set(_CATALOGUE_AT_VERSION_25) - _ADMIN_ONLY_AT_VERSION_25 - _EDITOR_ONLY_AT_VERSION_25
    else:
        return set()
    picked = {key for key in (_RENAMED.get(key, key) for key in stored) if key in allowed}
    picked |= {_IMPLIED_AT_VERSION_25[key] for key in picked if key in _IMPLIED_AT_VERSION_25}
    return {key for key in picked if key in allowed}


def _own_sets_to_changes(conn: sqlite3.Connection) -> None:
    unread = "SELECT id FROM users WHERE custom_role_id IS NOT NULL OR role IN ('admin', 'owner')"
    for table in ("user_permissions", "user_category_access", "user_allowed_categories"):
        if table_exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE user_id IN ({unread})")
    if not table_exists(conn, "user_category_access"):
        conn.execute("DELETE FROM user_permissions")
        return
    conn.execute("DELETE FROM user_permissions WHERE user_id NOT IN (SELECT user_id FROM user_category_access)")
    owners = tuples(conn, "SELECT u.id, COALESCE(u.role, 'user') FROM users u WHERE u.id IN "
                          "(SELECT user_id FROM user_category_access) ORDER BY u.id")
    for user_id, role in owners:
        stored = {key for (key,) in tuples(conn, "SELECT permission_key FROM user_permissions WHERE user_id = ?",
                                           (user_id,))}
        held = _held(role, stored)
        conn.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        conn.executemany("INSERT INTO user_permissions (user_id, permission_key, granted) VALUES (?, ?, ?)",
                         [(user_id, key, int(key in held)) for key in _CATALOGUE_AT_VERSION_25])


def upgrade(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "user_permissions") and "granted" not in column_names(conn, "user_permissions"):
        add_columns(conn, "user_permissions", {"granted": "INTEGER NOT NULL DEFAULT 1 CHECK (granted IN (0, 1))"})
        if table_exists(conn, "users"):
            _own_sets_to_changes(conn)
    if table_exists(conn, "site_settings"):
        add_columns(conn, "site_settings", {"access_requests_enabled": "INTEGER NOT NULL DEFAULT 0"})
    if not (table_exists(conn, "users") and table_exists(conn, "categories")):
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS access_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "scope TEXT NOT NULL CHECK (scope IN ('wiki', 'category')), "
        "category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE, "
        "page_id INTEGER REFERENCES pages(id) ON DELETE SET NULL, "
        "message TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'declined')), "
        "created_at TEXT NOT NULL, "
        "decided_by TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "decided_at TEXT, "
        "CHECK ((scope = 'category') = (category_id IS NOT NULL)))"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_access_requests_open "
                 "ON access_requests(user_id, scope, IFNULL(category_id, 0)) WHERE status = 'pending'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_status ON access_requests(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_user ON access_requests(user_id, created_at)")
