"""Permission definitions and defaults for custom role system."""

# Permission categories and their individual permissions
# Each permission is identified by a unique key

# Format: (key, label, description, default_for_editors, default_for_users)
PERMISSIONS = {
    # Page Permissions ===
    "pages": {
        "label": "Page Permissions",
        "permissions": [
            ("page.view_all", "View All Pages", "View all pages including deindexed ones", True, True),
            ("page.view_deindexed", "View Deindexed Pages", "View pages marked as deindexed", True, False),
            ("page.create", "Create Pages", "Create new wiki pages", True, False),
            ("page.edit_all", "Edit All Pages", "Edit any page in allowed categories", True, False),
            ("page.delete", "Delete Pages", "Delete wiki pages", False, False),
            ("page.edit_metadata", "Edit Page Metadata", "Change page title, slug, category", True, False),
            ("page.deindex", "Deindex Pages", "Mark pages as deindexed/hidden", False, False),
            ("page.export_pdf", "Export as PDF", "Download pages as PDF files", False, False),
        ],
    },

    # Category Permissions ===
    "categories": {
        "label": "Category Permissions",
        "permissions": [
            ("category.view_all", "View All Categories", "View all category pages", True, True),
            ("category.create", "Create Categories", "Create new categories", True, False),
            ("category.edit", "Edit Categories", "Rename and modify categories", True, False),
            ("category.delete", "Delete Categories", "Delete categories", True, False),
            ("category.reorder", "Reorder Categories", "Change category sort order", True, False),
            ("category.manage_sequential", "Manage Sequential Navigation", "Enable/disable sequential navigation in categories", True, False),
        ],
    },

    # Page History Permissions ===
    "history": {
        "label": "Page History",
        "permissions": [
            ("history.view", "View History", "View page edit history", True, True),
            ("history.revert", "Revert Changes", "Revert pages to previous versions", False, False),
            ("history.delete", "Delete History", "Delete individual history entries", False, False),
            ("history.transfer", "Transfer Attribution", "Transfer page attribution to another user", False, False),
        ],
    },

    # Draft Permissions ===
    "drafts": {
        "label": "Draft Management",
        "permissions": [
            ("draft.create", "Create Drafts", "Save page drafts", True, False),
            ("draft.view_own", "View Own Drafts", "View your own drafts", True, False),
            ("draft.delete_own", "Delete Own Drafts", "Delete your own drafts", True, False),
            ("draft.transfer", "Transfer Drafts", "Transfer drafts to other users", False, False),
        ],
    },

    # Attachment Permissions ===
    "attachments": {
        "label": "File Attachments",
        "permissions": [
            ("attachment.upload", "Upload Files", "Upload files attached to pages", True, False),
            ("attachment.view", "View Attachments", "View and download attachments", True, True),
            ("attachment.delete_own", "Delete Own Attachments", "Delete attachments you uploaded", True, False),
            ("attachment.delete_any", "Delete Any Attachment", "Delete any user's attachments", False, False),
        ],
    },

    # Tag Permissions ===
    "tags": {
        "label": "Page Tags",
        "permissions": [
            ("tag.edit_difficulty", "Edit Difficulty Tags", "Set difficulty tags on pages", True, False),
            ("tag.edit_custom", "Edit Custom Tags", "Set custom tags on pages", True, False),
        ],
    },

    # User Profile Permissions ===
    "profiles": {
        "label": "User Profiles",
        "permissions": [
            ("profile.view", "View User Profiles", "View public user profiles", True, True),
            ("profile.edit_own", "Edit Own Profile", "Edit your own profile page", True, True),
        ],
    },

    # Chat Permissions ===
    "chat": {
        "label": "Chat & Messaging",
        "permissions": [
            ("chat.dm", "Direct Messages", "Send and receive direct messages", True, True),
            ("chat.group", "Group Chats", "Join and participate in group chats", True, True),
            ("chat.create_group", "Create Group Chats", "Create new group chats", True, True),
            ("chat.upload", "Upload Chat Attachments", "Upload files in chats", True, True),
        ],
    },

    # Search & Navigation ===
    "search": {
        "label": "Search & Navigation",
        "permissions": [
            ("search.pages", "Search Pages", "Search wiki content", True, True),
            ("search.users", "Search Users", "Search for users", True, True),
        ],
    },

    # Invite Code Permissions ===
    "invites": {
        "label": "Invite Codes",
        "permissions": [
            ("invite.generate", "Generate Invite Codes", "Create invite codes for new users", False, False),
            ("invite.view", "View Invite Codes", "View list of invite codes", False, False),
            ("invite.delete", "Delete Invite Codes", "Delete unused invite codes", False, False),
        ],
    },

    # Kanban Permissions ===
    "kanban": {
        "label": "Kanban Boards",
        "permissions": [
            ("kanban.view", "View Kanban Boards", "Access and view shared kanban boards", True, True),
            ("kanban.create", "Create Kanban Boards", "Create and manage your own boards", True, False),
        ],
    },

    # Canvas Permissions ===
    "canvas": {
        "label": "Visual Canvas",
        "permissions": [
            ("canvas.view", "View Canvases", "Access and view shared visual canvases", True, True),
            ("canvas.create", "Create Canvases", "Create and manage your own canvases", True, False),
        ],
    },

    # Assessment Permissions ===
    "assessments": {
        "label": "Assessments & Quizzes",
        "permissions": [
            ("assessment.view", "Take Assessments", "View and complete page-linked assessments", True, True),
            ("assessment.manage", "Manage Assessments", "Create and edit assessments for pages", True, False),
        ],
    },

    # Custom Page Permissions ===
    # Published custom pages are public by design (anonymous visitors can
    # open them even on a private wiki), so there is no "view" permission to
    # grant or revoke.  Managing them is reserved for admins: see
    # ADMIN_ONLY_PERMISSION_KEYS below.
    "custom_pages": {
        "label": "Custom Pages",
        "permissions": [
            ("custom_page.manage", "Manage Custom Pages", "Create, edit, and delete site-wide custom pages (admins only)", False, False),
        ],
    },

    # Contribution Permissions ===
    "contributions": {
        "label": "Contributions",
        "permissions": [
            ("contribution.propose", "Propose Edits", "Propose page edits for admin review when contribution approval is enabled", False, True),
            ("contribution.review", "Review Contributions", "Approve or deny pending contributions from other users", False, False),
        ],
    },
}

EDITOR_ONLY_PERMISSION_KEYS = {
    "page.create",
    "page.edit_all",
    "page.delete",
    "page.edit_metadata",
    "page.deindex",
    "category.create",
    "category.edit",
    "category.delete",
    "category.reorder",
    "category.manage_sequential",
    "history.revert",
    "history.delete",
    "history.transfer",
    "draft.create",
    "draft.view_own",
    "draft.delete_own",
    "draft.transfer",
    "attachment.upload",
    "attachment.delete_own",
    "attachment.delete_any",
    "tag.edit_difficulty",
    "tag.edit_custom",
    "invite.generate",
    "invite.view",
    "invite.delete",
    "contribution.review",
}

# Permissions that only the admin and owner roles hold.  They are never
# assignable to editors or users, through custom roles or per-user settings,
# and has_permission() refuses them for those roles even if an old role row
# still lists them.  custom_page.manage is here because a custom page is
# served at any unreserved path of the wiki origin, can redirect visitors to
# any site and publishes uploaded files to anonymous visitors, which is
# admin-level power.
ADMIN_ONLY_PERMISSION_KEYS = {
    "custom_page.manage",
}

PERMISSION_IMPLICATIONS = {
    "page.view_deindexed": {"page.view_all"},
    "page.create": {"page.view_all"},
    "page.edit_all": {"page.view_all"},
    "page.delete": {"page.view_all"},
    "page.edit_metadata": {"page.view_all"},
    "page.deindex": {"page.view_all"},
    "category.create": {"category.view_all"},
    "category.edit": {"category.view_all"},
    "category.delete": {"category.view_all"},
    "category.reorder": {"category.view_all"},
    "category.manage_sequential": {"category.view_all"},
    "contribution.propose": {"page.view_all"},
    "contribution.review": {"page.view_all"},
}

# Mapping of permission keys to the plugin that gates them.
# When a plugin is disabled, permissions in this mapping are effectively revoked.
PERMISSION_PLUGIN_MAP = {
    "history.view": "page_history",
    "history.revert": "page_history",
    "history.delete": "page_history",
    "history.transfer": "page_history",
    "draft.create": "drafts",
    "draft.view_own": "drafts",
    "draft.delete_own": "drafts",
    "draft.transfer": "drafts",
    "attachment.upload": "attachments",
    "attachment.view": "attachments",
    "attachment.delete_own": "attachments",
    "attachment.delete_any": "attachments",
    "tag.edit_difficulty": "difficulty_tags",
    "tag.edit_custom": "difficulty_tags",
    "profile.view": "user_profiles",
    "profile.edit_own": "user_profiles",
    "chat.dm": "chat",
    "chat.group": "chat",
    "chat.create_group": "chat",
    "chat.upload": "chat",
    "kanban.view": "kanban",
    "kanban.create": "kanban",
    "canvas.view": "canvas",
    "canvas.create": "canvas",
    "assessment.view": "assessments",
    "assessment.manage": "assessments",
    "custom_page.manage": "custom_pages",
}

# Shortcut to get all permission keys
def get_all_permission_keys():
    """Return a list of all permission keys."""
    keys = []
    for category in PERMISSIONS.values():
        for perm in category["permissions"]:
            keys.append(perm[0])
    return keys


def get_assignable_permission_keys(role):
    """Return permission keys that may be assigned to the given role."""
    all_keys = set(get_all_permission_keys())
    if role in ("admin", "owner"):
        return all_keys
    if role == "editor":
        return all_keys - ADMIN_ONLY_PERMISSION_KEYS
    if role == "user":
        return all_keys - EDITOR_ONLY_PERMISSION_KEYS - ADMIN_ONLY_PERMISSION_KEYS
    return set()


def is_permission_assignable_to_role(permission_key, role):
    """Return True if a permission may be assigned to the given role."""
    return permission_key in get_assignable_permission_keys(role)


def normalize_permission_keys(permission_keys):
    """Return permission keys with implied dependencies applied."""
    normalized = set(permission_keys)
    stack = list(normalized)
    while stack:
        key = stack.pop()
        for implied_key in PERMISSION_IMPLICATIONS.get(key, set()):
            if implied_key not in normalized:
                normalized.add(implied_key)
                stack.append(implied_key)
    return normalized


def sanitize_permission_keys(role, permission_keys):
    """Filter a permission selection down to the permissions valid for *role*."""
    allowed = get_assignable_permission_keys(role)
    return normalize_permission_keys(set(permission_keys) & allowed)

def get_default_permissions(role):
    """Return default permissions for a role.

    Args:
        role: 'editor' or 'user'

    Returns:
        Set of permission keys that should be enabled by default
    """
    if role == "editor":
        index = 3  # default_for_editors
    elif role == "user":
        index = 4  # default_for_users
    else:
        return set()

    defaults = set()
    for category in PERMISSIONS.values():
        for perm in category["permissions"]:
            if perm[index]:
                defaults.add(perm[0])
    return defaults

def get_permission_label(key):
    """Get the human-readable label for a permission key."""
    for category in PERMISSIONS.values():
        for perm in category["permissions"]:
            if perm[0] == key:
                return perm[1]
    return key

def get_permission_description(key):
    """Get the description for a permission key."""
    for category in PERMISSIONS.values():
        for perm in category["permissions"]:
            if perm[0] == key:
                return perm[2]
    return ""

def group_permissions_by_category(role=None):
    """Return permissions grouped by category for UI display."""
    if role is None:
        return PERMISSIONS

    allowed = get_assignable_permission_keys(role)
    grouped = {}
    for key, category in PERMISSIONS.items():
        category_permissions = [
            perm for perm in category["permissions"] if perm[0] in allowed
        ]
        if category_permissions:
            grouped[key] = {
                "label": category["label"],
                "permissions": category_permissions,
            }
    return grouped
