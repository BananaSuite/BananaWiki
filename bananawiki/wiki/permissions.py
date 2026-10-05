"""Roles, permission keys and category access.

Model
-----
* Base roles, weakest to strongest: ``user`` < ``editor`` < ``admin`` < ``owner``.
  Admins and owners hold every permission and see every category.
* Users and editors get a permission set from, in order of precedence:
  1. their custom role (``users.custom_role_id``), or
  2. per-user overrides (present when the user has ``user_category_access`` rows), or
  3. the defaults for their base role.
* Category access is either unrestricted or an allow-list of category ids,
  separately for reading and (editors only) writing. Write access implies read.
  Pages without a category are hidden from restricted readers/writers.
* A permission that belongs to a disabled feature is never granted.

Unlike 1.4, every key in the catalogue is enforced by the routes that need it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

ROLES = ("user", "editor", "admin", "owner")
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}
ADMIN_ROLES = frozenset({"admin", "owner"})
EDITOR_ROLES = frozenset({"editor", "admin", "owner"})


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    description: str
    editor_default: bool
    user_default: bool
    feature: str | None = None
    editor_only: bool = False
    admin_only: bool = False


def _p(key, label, description, editor, user, *, feature=None, editor_only=False, admin_only=False):
    return Permission(key, label, description, editor, user, feature, editor_only, admin_only)


GROUPS: dict[str, tuple[str, tuple[Permission, ...]]] = {
    "pages": ("Pages", (
        _p("page.view_all", "View all pages", "Read every page in readable categories", True, True),
        _p("page.view_deindexed", "View hidden pages", "Read pages hidden from navigation and search", True, False),
        _p("page.create", "Create pages", "Create new pages", True, False, editor_only=True),
        _p("page.edit_all", "Edit pages", "Edit any page in writable categories", True, False, editor_only=True),
        _p("page.delete", "Delete pages", "Delete pages", False, False, editor_only=True),
        _p("page.edit_metadata", "Edit page details", "Change a page's title, address and category", True, False,
           editor_only=True),
        _p("page.deindex", "Hide pages", "Hide pages from navigation and search", False, False, editor_only=True),
        _p("page.export_pdf", "Export as PDF", "Download pages as PDF files", False, False),
    )),
    "categories": ("Categories", (
        _p("category.view_all", "View all categories", "See every readable category", True, True),
        _p("category.create", "Create categories", "Create categories", True, False, editor_only=True),
        _p("category.edit", "Rename categories", "Rename and edit categories", True, False, editor_only=True),
        _p("category.delete", "Delete categories", "Delete categories", True, False, editor_only=True),
        _p("category.reorder", "Reorder categories", "Change the order and nesting of categories", True, False,
           editor_only=True),
        _p("category.manage_sequential", "Sequential navigation", "Turn sequential navigation on or off", True,
           False, editor_only=True),
    )),
    "history": ("Page history", (
        _p("history.view", "View history", "Read earlier versions of pages", True, True, feature="page_history"),
        _p("history.revert", "Revert changes", "Restore an earlier version of a page", False, False,
           feature="page_history", editor_only=True),
        _p("history.delete", "Delete history", "Delete entries from a page's history", False, False,
           feature="page_history", editor_only=True),
        _p("history.transfer", "Transfer attribution", "Credit an edit to another user", False, False,
           feature="page_history", editor_only=True),
    )),
    "drafts": ("Drafts", (
        _p("draft.create", "Save drafts", "Save unfinished edits as drafts", True, False, feature="drafts",
           editor_only=True),
        _p("draft.view_own", "View own drafts", "List your own drafts", True, False, feature="drafts",
           editor_only=True),
        _p("draft.delete_own", "Delete own drafts", "Delete your own drafts", True, False, feature="drafts",
           editor_only=True),
        _p("draft.transfer", "Transfer drafts", "Hand a draft to another user", False, False, feature="drafts",
           editor_only=True),
    )),
    "attachments": ("Attachments", (
        _p("attachment.upload", "Upload files", "Attach files to pages", True, False, feature="attachments",
           editor_only=True),
        _p("attachment.view", "Download files", "Download page attachments", True, True, feature="attachments"),
        _p("attachment.delete_own", "Delete own files", "Delete attachments you uploaded", True, False,
           feature="attachments", editor_only=True),
        _p("attachment.delete_any", "Delete any file", "Delete anyone's attachments", False, False,
           feature="attachments", editor_only=True),
    )),
    "tags": ("Page tags", (
        _p("tag.edit_difficulty", "Set difficulty tags", "Set a page's difficulty tag", True, False,
           feature="difficulty_tags", editor_only=True),
        _p("tag.edit_custom", "Set custom tags", "Set a page's custom tag", True, False, feature="difficulty_tags",
           editor_only=True),
    )),
    "profiles": ("Profiles", (
        _p("profile.view", "View profiles", "Open other people's profile pages", True, True, feature="user_profiles"),
        _p("profile.edit_own", "Edit own profile", "Edit your own profile page", True, True,
           feature="user_profiles"),
    )),
    "chat": ("Chat", (
        _p("chat.dm", "Direct messages", "Send and receive direct messages", True, True, feature="chat"),
        _p("chat.group", "Group chats", "Take part in group chats", True, True, feature="chat"),
        _p("chat.create_group", "Create group chats", "Create group chats", True, True, feature="chat"),
        _p("chat.upload", "Send files", "Send files in chats", True, True, feature="chat"),
    )),
    "search": ("Search", (
        _p("search.pages", "Search pages", "Search the wiki", True, True),
        _p("search.users", "Search people", "Search the member list", True, True),
    )),
    "invites": ("Invite codes", (
        _p("invite.generate", "Create invite codes", "Create invite codes", False, False, editor_only=True),
        _p("invite.view", "View invite codes", "List invite codes", False, False, editor_only=True),
        _p("invite.delete", "Delete invite codes", "Delete unused invite codes", False, False, editor_only=True),
    )),
    "kanban": ("Kanban boards", (
        _p("kanban.view", "View boards", "Open boards shared with you", True, True, feature="kanban"),
        _p("kanban.create", "Create boards", "Create and manage your own boards", True, False, feature="kanban"),
    )),
    "canvas": ("Canvases", (
        _p("canvas.view", "View canvases", "Open canvases shared with you", True, True, feature="canvas"),
        _p("canvas.create", "Create canvases", "Create and manage your own canvases", True, False,
           feature="canvas"),
    )),
    "assessments": ("Assessments", (
        _p("assessment.view", "Take assessments", "Answer the quizzes attached to pages", True, True,
           feature="assessments"),
        _p("assessment.manage", "Manage assessments", "Create and edit quizzes on pages", True, False,
           feature="assessments"),
    )),
    "custom_pages": ("Custom pages", (
        _p("custom_page.manage", "Manage custom pages", "Create, edit and delete custom pages", False, False,
           feature="custom_pages", admin_only=True),
    )),
    "contributions": ("Contributions", (
        _p("contribution.propose", "Propose edits", "Suggest edits for review", False, True),
        _p("contribution.review", "Review contributions", "Approve or deny suggested edits", False, False,
           editor_only=True),
    )),
}

CATALOGUE: dict[str, Permission] = {perm.key: perm for _, perms in GROUPS.values() for perm in perms}

IMPLICATIONS: dict[str, frozenset[str]] = {
    "page.view_deindexed": frozenset({"page.view_all"}),
    "page.create": frozenset({"page.view_all"}),
    "page.edit_all": frozenset({"page.view_all"}),
    "page.delete": frozenset({"page.view_all"}),
    "page.edit_metadata": frozenset({"page.view_all"}),
    "page.deindex": frozenset({"page.view_all"}),
    "category.create": frozenset({"category.view_all"}),
    "category.edit": frozenset({"category.view_all"}),
    "category.delete": frozenset({"category.view_all"}),
    "category.reorder": frozenset({"category.view_all"}),
    "category.manage_sequential": frozenset({"category.view_all"}),
    "contribution.propose": frozenset({"page.view_all"}),
    "contribution.review": frozenset({"page.view_all"}),
    "history.revert": frozenset({"history.view"}),
    "history.delete": frozenset({"history.view"}),
}


def role_at_least(role: str | None, minimum: str) -> bool:
    return ROLE_RANK.get(role or "", -1) >= ROLE_RANK[minimum]


def assignable(role: str) -> frozenset[str]:
    """Permission keys that may be granted to someone with base *role*."""
    if role in ADMIN_ROLES:
        return frozenset(CATALOGUE)
    keys = {k for k, p in CATALOGUE.items() if not p.admin_only}
    if role == "user":
        keys = {k for k in keys if not CATALOGUE[k].editor_only}
    elif role != "editor":
        return frozenset()
    return frozenset(keys)


def with_implications(keys: Iterable[str]) -> set[str]:
    result = set(keys)
    stack = list(result)
    while stack:
        for implied in IMPLICATIONS.get(stack.pop(), ()):
            if implied not in result:
                result.add(implied)
                stack.append(implied)
    return result


def sanitize(role: str, keys: Iterable[str]) -> set[str]:
    """Restrict a selection to what *role* may hold, adding implied keys."""
    allowed = assignable(role)
    return {k for k in with_implications(k for k in keys if k in allowed) if k in allowed}


def defaults(role: str) -> frozenset[str]:
    if role == "editor":
        return frozenset(k for k, p in CATALOGUE.items() if p.editor_default)
    if role == "user":
        return frozenset(k for k, p in CATALOGUE.items() if p.user_default)
    if role in ADMIN_ROLES:
        return frozenset(CATALOGUE)
    return frozenset()


def grouped_for(role: str | None = None) -> list[tuple[str, str, list[Permission]]]:
    """Catalogue grouped for display, optionally limited to *role*."""
    allowed = assignable(role) if role else frozenset(CATALOGUE)
    out = []
    for group_id, (label, perms) in GROUPS.items():
        visible = [p for p in perms if p.key in allowed]
        if visible:
            out.append((group_id, label, visible))
    return out


@dataclass
class CategoryAccess:
    restricted: bool = False
    allowed: frozenset[int] = field(default_factory=frozenset)

    def permits(self, category_id: int | None) -> bool:
        if not self.restricted:
            return True
        return category_id is not None and int(category_id) in self.allowed


@dataclass
class Grants:
    """Everything the permission checks need to know about one user."""

    role: str
    keys: frozenset[str]
    read: CategoryAccess
    write: CategoryAccess
    chat_disabled: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES


def load_grants(db: Any, user: dict[str, Any]) -> Grants:
    """Resolve a user's effective grants from the database (one to three queries).

    Stored keys get their implied keys added, as the admin forms do when
    saving, so rows carried over from 1.4 that hold ``page.edit_all`` without
    ``page.view_all`` keep reading the pages they edit.
    """
    role = user.get("role") or "user"
    chat_disabled = bool(user.get("chat_disabled"))
    if role in ADMIN_ROLES:
        return Grants(role, frozenset(CATALOGUE), CategoryAccess(), CategoryAccess(), chat_disabled)

    custom_role_id = user.get("custom_role_id")
    if custom_role_id:
        custom = db.one(
            "SELECT id, base_role, read_restricted, write_restricted FROM custom_roles WHERE id = ?",
            (custom_role_id,),
        )
        if custom:
            keys = frozenset(db.column(
                "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (custom["id"],)
            ))
            cats = db.all(
                "SELECT category_id, access_type FROM custom_role_categories WHERE role_id = ?", (custom["id"],)
            )
            read_ids = {c["category_id"] for c in cats if c["access_type"] == "read"}
            write_ids = {c["category_id"] for c in cats if c["access_type"] == "write"}
            read_restricted = bool(custom["read_restricted"])
            if custom["write_restricted"]:
                # Restricted writers only ever see what they can read or write.
                read_restricted = True
                read_ids |= write_ids
            write = CategoryAccess(bool(custom["write_restricted"]), frozenset(write_ids))
            read = CategoryAccess(read_restricted, frozenset(read_ids | (write_ids if read_restricted else set())))
            return Grants(role, frozenset(sanitize(role, keys)), read, write, chat_disabled)
        # Dangling custom role: fall back to the base role's defaults.
        return Grants(role, defaults(role), CategoryAccess(), CategoryAccess(), chat_disabled)

    access_rows = db.all(
        "SELECT access_type, restricted FROM user_category_access WHERE user_id = ?", (user["id"],)
    )
    if not access_rows:
        return Grants(role, defaults(role), CategoryAccess(), CategoryAccess(), chat_disabled)

    keys = frozenset(db.column("SELECT permission_key FROM user_permissions WHERE user_id = ?", (user["id"],)))
    restricted = {row["access_type"]: bool(row["restricted"]) for row in access_rows}
    cats = db.all("SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?", (user["id"],))
    read_ids = frozenset(c["category_id"] for c in cats if c["access_type"] == "read")
    write_ids = frozenset(c["category_id"] for c in cats if c["access_type"] == "write")
    write = CategoryAccess(restricted.get("write", False), write_ids)
    read = CategoryAccess(restricted.get("read", False), read_ids | write_ids)
    return Grants(role, frozenset(sanitize(role, keys)), read, write, chat_disabled)


def grants_permission(grants: Grants, key: str, feature_enabled) -> bool:
    """Decide *key* for *grants*; *feature_enabled(feature_id)* reports feature flags."""
    permission = CATALOGUE.get(key)
    if permission is None:
        return False
    if permission.feature and not feature_enabled(permission.feature):
        return False
    if grants.is_admin:
        return True
    if key.startswith("chat.") and grants.chat_disabled:
        return False
    return key in grants.keys


def can_read_category(grants: Grants, category_id: int | None) -> bool:
    if grants.is_admin:
        return True
    return grants.read.permits(category_id) or can_write_category(grants, category_id)


def can_write_category(grants: Grants, category_id: int | None) -> bool:
    if grants.is_admin:
        return True
    if grants.role != "editor":
        return False
    return grants.write.permits(category_id)
