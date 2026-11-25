"""Roles, permission keys and category access: the one place that decides who may do what.

Model
-----
* Base roles, weakest to strongest: ``user`` < ``editor`` < ``admin`` < ``owner``.
  Admins and owners hold every permission and see every category.
* Users and editors start from their custom role (``users.custom_role_id``)
  when they have one, otherwise from the defaults of their base role: the
  built-in defaults below, changed by the wiki's administrators on the base
  role's page (``base_role_permissions`` holds only the differences).
* On top of that, one person may have changes of their own
  (``user_permissions``: ``granted`` 1 adds a key, 0 takes it away), so they
  keep following their role for everything else. Before version 26 a person's
  own permissions replaced the role's whole set; version 26 stored those sets
  as changes to every key, so nobody's access changed.
* Pages, Kanban boards and canvases share the same verbs: view, create, edit
  own, delete own, (share own,) edit all, delete all. "Own" means created by
  the person; "all" means every item they can open. Who may see a board or a
  canvas beyond its creator is decided by the item itself (visibility and
  shares, see :mod:`.sharing`), never by a site setting.
* Category access is either unrestricted or an allow-list of category ids,
  separately for reading and writing. Writing to a listed category implies
  reading it; unrestricted writing means writing wherever one may read.
  Pages without a category are hidden from restricted readers/writers.
  A custom role may limit both; a person's own list (``user_category_access``
  and ``user_allowed_categories``) limits them where their role does not, and
  adds to their role's list where it does.
* A permission that belongs to a disabled feature is never granted.

Which permissions a ``user`` may hold: everything except ``editor_only``
ones, which are the powers over the wiki's structure and over other people's
records (categories, page history, hiding pages, reviewing proposed edits,
taking over drafts, deleting others' files, invite codes). Working on content,
one's own or everyone's, is open to both base roles. ``admin_only`` keys are
held by administrators alone.

Every key in the catalogue is enforced by the routes that need it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

ROLES = ("user", "editor", "admin", "owner")
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}
ADMIN_ROLES = frozenset({"admin", "owner"})
EDITOR_ROLES = frozenset({"editor", "admin", "owner"})
#: Base roles whose defaults the wiki's administrators may change.
BASE_ROLES = ("user", "editor")


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


def _item_verbs(prefix: str, feature: str | None, things: str, *, share: bool) -> tuple[Permission, ...]:
    """The verbs every kind of content has, with the same defaults."""
    verbs = [
        _p(f"{prefix}.view", f"View {things}", f"Open {things} visible to members or shared with you",
           True, True, feature=feature),
        _p(f"{prefix}.create", f"Create {things}", f"Create new {things}", True, True, feature=feature),
        _p(f"{prefix}.edit_own", f"Edit own {things}", f"Change the {things} you created", True, True,
           feature=feature),
        _p(f"{prefix}.delete_own", f"Delete own {things}", f"Delete or archive the {things} you created", True,
           True, feature=feature),
    ]
    if share:
        verbs.append(_p(f"{prefix}.share_own", f"Share own {things}",
                        f"Choose who sees and edits the {things} you created, or hand them over", True, True,
                        feature=feature))
    verbs += [
        _p(f"{prefix}.edit_all", f"Edit all {things}", f"Change every one of the {things} you can open", True,
           False, feature=feature),
        _p(f"{prefix}.delete_all", f"Delete all {things}", f"Delete or archive any of the {things} you can open",
           False, False, feature=feature),
    ]
    return tuple(verbs)


GROUPS: dict[str, tuple[str, tuple[Permission, ...]]] = {
    "pages": ("Pages", (
        _p("page.view_all", "View pages", "Read the pages in the categories you may read", True, True),
        _p("page.view_deindexed", "View hidden pages", "Read pages hidden from navigation and search", True, False),
        _p("page.create", "Create pages", "Create new pages", True, True),
        _p("page.edit_own", "Edit own pages", "Change the pages you created, their title, address and category too",
           True, True),
        _p("page.delete_own", "Delete own pages", "Delete the pages you created", True, True),
        _p("page.edit_all", "Edit all pages", "Change every page in the categories you may write to", True, False),
        _p("page.delete_all", "Delete all pages", "Delete any page you can edit", False, False),
        _p("page.edit_metadata", "Rename and move pages",
           "Change the title, address and category of any page you can edit", True, False),
        _p("page.deindex", "Hide pages", "Hide pages from navigation and search", False, False, editor_only=True),
        _p("page.use_builder", "Use the page builder", "Lay out the pages you can edit with blocks", False, False,
           feature="page_builder"),
        _p("page.export_pdf", "Export as PDF", "Download pages as PDF files", False, False),
    )),
    "categories": ("Categories", (
        _p("category.view_all", "View categories", "See the categories you may read in lists and search", True,
           True),
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
        _p("draft.create", "Save drafts", "Save unfinished edits as drafts", True, True, feature="drafts"),
        _p("draft.view_own", "View own drafts", "List your own drafts", True, True, feature="drafts"),
        _p("draft.delete_own", "Delete own drafts", "Delete your own drafts", True, True, feature="drafts"),
        _p("draft.transfer", "Take over drafts", "Take over another user's draft", False, False, feature="drafts",
           editor_only=True),
    )),
    "attachments": ("Attachments", (
        _p("attachment.upload", "Upload files", "Attach files to the pages you can edit", True, True,
           feature="attachments"),
        _p("attachment.view", "Download files", "Download page attachments", True, True, feature="attachments"),
        _p("attachment.delete_own", "Delete own files", "Delete attachments you uploaded", True, True,
           feature="attachments"),
        _p("attachment.delete_any", "Delete any file", "Delete anyone's attachments", False, False,
           feature="attachments", editor_only=True),
    )),
    "tags": ("Page tags", (
        _p("tag.edit_difficulty", "Set difficulty tags", "Set a page's difficulty tag", True, False,
           feature="difficulty_tags"),
        _p("tag.edit_custom", "Set custom tags", "Set a page's custom tag", True, False, feature="difficulty_tags"),
    )),
    "contributions": ("Proposed edits", (
        _p("contribution.propose", "Propose edits", "Suggest changes to pages you cannot edit", False, True),
        _p("contribution.review", "Review proposed edits", "Approve or deny suggested changes", False, False,
           editor_only=True),
    )),
    "kanban": ("Kanban boards", _item_verbs("kanban", "kanban", "boards", share=True)),
    "canvas": ("Canvases", _item_verbs("canvas", "canvas", "canvases", share=True)),
    "assessments": ("Assessments", (
        _p("assessment.view", "Take assessments", "Answer the quizzes attached to pages", True, True,
           feature="assessments"),
        _p("assessment.manage", "Manage assessments", "Create and edit quizzes on pages", True, False,
           feature="assessments"),
    )),
    "profiles": ("Profiles", (
        _p("profile.view", "View profiles", "Open other people's profile pages", True, True, feature="user_profiles"),
        _p("profile.edit_own", "Edit own profile", "Edit your own profile page", True, True,
           feature="user_profiles"),
    )),
    "chat": ("Chat", (
        _p("chat.dm", "Direct messages", "Send and receive direct messages", True, True, feature="chat"),
        _p("chat.start_dm", "Start conversations", "Write to someone first", True, True, feature="chat"),
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
    "custom_pages": ("Custom pages", (
        _p("custom_page.manage", "Manage custom pages", "Create, edit and delete custom pages", False, False,
           feature="custom_pages", admin_only=True),
    )),
}

#: Keys renamed since 1.4/2.0: stored rows are migrated (schema version 23) and checks by
#: the old name still answer for the new one, so plugins written against them keep working.
RENAMED = {"page.delete": "page.delete_all"}

CATALOGUE: dict[str, Permission] = {perm.key: perm for _, perms in GROUPS.values() for perm in perms}

def _implications() -> dict[str, frozenset[str]]:
    found: dict[str, frozenset[str]] = {
        "page.view_deindexed": frozenset({"page.view_all"}),
        "page.create": frozenset({"page.view_all"}),
        "page.edit_own": frozenset({"page.view_all"}),
        "page.delete_own": frozenset({"page.view_all"}),
        "page.edit_all": frozenset({"page.view_all"}),
        "page.delete_all": frozenset({"page.view_all"}),
        "page.edit_metadata": frozenset({"page.view_all"}),
        "page.deindex": frozenset({"page.view_all"}),
        "page.use_builder": frozenset({"page.view_all"}),
        "category.create": frozenset({"category.view_all"}),
        "category.edit": frozenset({"category.view_all"}),
        "category.delete": frozenset({"category.view_all"}),
        "category.reorder": frozenset({"category.view_all"}),
        "category.manage_sequential": frozenset({"category.view_all"}),
        "contribution.propose": frozenset({"page.view_all"}),
        "contribution.review": frozenset({"page.view_all"}),
        "history.revert": frozenset({"history.view"}),
        "chat.start_dm": frozenset({"chat.dm"}),
        "chat.create_group": frozenset({"chat.group"}),
        "history.delete": frozenset({"history.view"}),
    }
    for prefix in ("kanban", "canvas"):
        for verb in ("create", "edit_own", "delete_own", "share_own", "edit_all", "delete_all"):
            found[f"{prefix}.{verb}"] = frozenset({f"{prefix}.view"})
    return found


IMPLICATIONS: dict[str, frozenset[str]] = _implications()


def canonical(key: str) -> str:
    return RENAMED.get(key, key)


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
    result = {canonical(k) for k in keys}
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
    return {k for k in with_implications(canonical(k) for k in keys if canonical(k) in allowed) if k in allowed}


def builtin_defaults(role: str) -> frozenset[str]:
    """The defaults a base role has until the wiki's administrators change them."""
    if role == "editor":
        return frozenset(k for k, p in CATALOGUE.items() if p.editor_default)
    if role == "user":
        return frozenset(k for k, p in CATALOGUE.items() if p.user_default)
    if role in ADMIN_ROLES:
        return frozenset(CATALOGUE)
    return frozenset()


#: The name before version 23 (plugins and tests call it): the built-in defaults, not the wiki's own.
defaults = builtin_defaults


def apply_changes(role: str, changes: dict[str, bool]) -> frozenset[str]:
    """The built-in defaults of *role* with the wiki's *changes* (``{key: granted}``) applied."""
    return changed(role, builtin_defaults(role), changes)


def changed(role: str, start: Iterable[str], changes: dict[str, bool]) -> frozenset[str]:
    """*start* with *changes* (``{key: granted}``) applied: base-role defaults, or one person's own changes."""
    keys = set(start)
    removed = set()
    for key, granted in changes.items():
        key = canonical(key)
        if key not in CATALOGUE:
            continue
        if granted:
            keys.add(key)
        else:
            removed.add(key)
    # A key taken away also goes from the defaults that imply it (no "start conversations" without messages).
    keys = {key for key in keys if not with_implications({key}) & removed}
    return frozenset(sanitize(role, keys) if role in BASE_ROLES else keys)


def role_defaults(db: Any, role: str) -> frozenset[str]:
    """The defaults of base *role* on this wiki (one query)."""
    if role not in BASE_ROLES:
        return builtin_defaults(role)
    rows = db.all("SELECT permission_key, granted FROM base_role_permissions WHERE role = ?", (role,))
    return apply_changes(role, {row["permission_key"]: bool(row["granted"]) for row in rows})


def changes_for(role: str, keys: Iterable[str], start: Iterable[str] | None = None) -> dict[str, bool]:
    """What to store so that *start* (default: *role*'s built-in defaults) becomes *keys*: the differences."""
    wanted = sanitize(role, keys)
    base = frozenset(builtin_defaults(role) if start is None else start) & assignable(role)
    return {key: key in wanted for key in assignable(role) if (key in wanted) != (key in base)}


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
    #: Where the person starts from: ``admin``, ``custom_role`` or ``defaults`` (of the base role).
    source: str = "defaults"
    #: The keys the person starts from, before their own changes.
    start: frozenset[str] = frozenset()
    #: The person has changes of their own (permissions or category lists).
    personal: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES


def combine_access(role_read: CategoryAccess, role_write: CategoryAccess, own: dict[str, bool],
                   read_ids: Iterable[int], write_ids: Iterable[int]) -> tuple[CategoryAccess, CategoryAccess]:
    """A person's category access: their role's limits, then their own list.

    *own* is ``{access_type: restricted}`` of the person's own settings. Where
    the role does not limit a kind of access, the person's own list does (as
    it did before custom roles could be combined with it); where the role
    limits it, the person's own list adds categories to the role's.
    """
    read_ids, write_ids = frozenset(read_ids), frozenset(write_ids)
    own_read, own_write = own.get("read", False), own.get("write", False)
    if not role_read.restricted and not role_write.restricted:
        # No role limits: the person's own settings alone, exactly as stored.
        return CategoryAccess(own_read, read_ids | write_ids), CategoryAccess(own_write, write_ids)
    extra_write = write_ids if own_write else frozenset()
    extra_read = (read_ids if own_read else frozenset()) | extra_write
    write = CategoryAccess(role_write.restricted or own_write,
                           (role_write.allowed if role_write.restricted else frozenset()) | extra_write)
    read = CategoryAccess(role_read.restricted or own_read,
                          (role_read.allowed if role_read.restricted else frozenset()) | extra_read)
    return read, write


def load_grants(db: Any, user: dict[str, Any], defaults: frozenset[str] | None = None) -> Grants:
    """Resolve a user's effective grants from the database (two to four queries).

    Stored keys get their implied keys added, as the admin forms do when
    saving, so rows carried over from 1.4 that hold ``page.edit_all`` without
    ``page.view_all`` keep reading the pages they edit. *defaults*, when
    given, are the base role's defaults already read for this request.
    """
    role = user.get("role") or "user"
    chat_disabled = bool(user.get("chat_disabled"))
    if role in ADMIN_ROLES:
        everything = frozenset(CATALOGUE)
        return Grants(role, everything, CategoryAccess(), CategoryAccess(), chat_disabled, "admin", everything)

    role_read, role_write, source = CategoryAccess(), CategoryAccess(), "defaults"
    custom_role_id = user.get("custom_role_id")
    custom = db.one(
        "SELECT id, base_role, read_restricted, write_restricted FROM custom_roles WHERE id = ?", (custom_role_id,)
    ) if custom_role_id else None
    if custom:
        keys = db.column("SELECT permission_key FROM custom_role_permissions WHERE role_id = ?", (custom["id"],))
        cats = db.all("SELECT category_id, access_type FROM custom_role_categories WHERE role_id = ?", (custom["id"],))
        read_ids = {c["category_id"] for c in cats if c["access_type"] == "read"}
        write_ids = {c["category_id"] for c in cats if c["access_type"] == "write"}
        read_restricted = bool(custom["read_restricted"])
        if custom["write_restricted"]:
            # Restricted writers only ever see what they can read or write.
            read_restricted = True
            read_ids |= write_ids
        role_write = CategoryAccess(bool(custom["write_restricted"]), frozenset(write_ids))
        role_read = CategoryAccess(read_restricted, frozenset(read_ids | (write_ids if read_restricted else set())))
        start = frozenset(sanitize(role, keys))
        source = "custom_role"
    else:
        # No custom role, or one that no longer exists: the base role's defaults.
        start = defaults if defaults is not None else role_defaults(db, role)

    own_rows = db.all(
        "SELECT 'key' AS kind, permission_key AS name, granted AS value FROM user_permissions WHERE user_id = ? "
        "UNION ALL SELECT 'access', access_type, restricted FROM user_category_access WHERE user_id = ?",
        (user["id"], user["id"]),
    )
    changes = {row["name"]: bool(row["value"]) for row in own_rows if row["kind"] == "key"}
    own = {row["name"]: bool(row["value"]) for row in own_rows if row["kind"] == "access"}
    keys = frozenset(sanitize(role, changed(role, start, changes))) if changes else start
    read, write = role_read, role_write
    if any(own.values()):
        cats = db.all("SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?", (user["id"],))
        read, write = combine_access(role_read, role_write, own,
                                     (c["category_id"] for c in cats if c["access_type"] == "read"),
                                     (c["category_id"] for c in cats if c["access_type"] == "write"))
    personal = keys != start or any(own.values())
    return Grants(role, frozenset(keys), read, write, chat_disabled, source, frozenset(start), personal)


def grants_permission(grants: Grants, key: str, feature_enabled) -> bool:
    """Decide *key* for *grants*; *feature_enabled(feature_id)* reports feature flags."""
    key = canonical(key)
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
    """Read access: the read list, plus the categories on a restricted write list."""
    if grants.is_admin:
        return True
    if grants.read.permits(category_id):
        return True
    return grants.write.restricted and grants.write.permits(category_id)


def can_write_category(grants: Grants, category_id: int | None) -> bool:
    """Write access: the write list, or wherever the person may read when writing is unrestricted."""
    if grants.is_admin:
        return True
    if grants.write.restricted:
        return grants.write.permits(category_id)
    return grants.read.permits(category_id)


def readable_category_ids(grants: Grants) -> frozenset[int] | None:
    """The categories *grants* may read, or None for all of them."""
    if grants.is_admin or not grants.read.restricted:
        return None
    ids = set(grants.read.allowed)
    if grants.write.restricted:
        ids |= grants.write.allowed
    return frozenset(ids)
