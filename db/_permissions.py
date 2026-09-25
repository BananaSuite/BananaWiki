"""Custom permission management for editors and users."""

from ._connection import get_db_context, retry_on_busy


def _get_permissions_cache():
    """Return the per-request permissions cache dict, or None outside a request context."""
    try:
        from flask import g
        cache = getattr(g, "_permissions_cache", None)
        if cache is None:
            g._permissions_cache = {}
        return g._permissions_cache
    except RuntimeError:
        # Outside of a Flask request context (e.g. tests, CLI)
        return None


@retry_on_busy
def get_user_permissions(user_id):
    """Get all custom permissions for a user.

    Permissions are resolved from the assigned custom role when present.
    If no custom role is assigned (or the assignment is stale), defaults
    are derived from the user's current base role.

    Results are cached on Flask's ``g`` for the duration of the current
    request so that repeated calls for the same user (e.g. when filtering
    a long sidebar) only hit the database once.

    Wrapped in :func:`retry_on_busy` because this runs on the hot path
    for any non-admin logged-in user: :func:`app.inject_globals` filters
    the sidebar categories through :func:`db.has_category_read_access`,
    which in turn calls this function on every page render.  A transient
    ``database is locked`` from a concurrent writer (periodic cleanup,
    nightly backup, another worker committing a page edit) used to
    surface as the "random 500 that goes away after a reload" pattern.

    Returns:
        dict: {
            'enabled_permissions': set of permission keys,
            'category_access': {
                'restricted': bool,
                'allowed_category_ids': list of category IDs (for read access)
            },
            'category_write_access': {
                'restricted': bool,
                'allowed_category_ids': list of category IDs (for write access)
            }
        }
    """
    # Cached for the life of the request: filtering a category or page listing
    # asks for the same user's permissions once per row otherwise.
    cache = _get_permissions_cache()
    if cache is not None and user_id in cache:
        return cache[user_id]

    with get_db_context() as conn:
        cur = conn.cursor()

        user_row = cur.execute(
            "SELECT role, custom_role_id FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        role = user_row["role"] if user_row else None
        custom_role_id = user_row["custom_role_id"] if user_row and "custom_role_id" in user_row.keys() else None

        # Custom role path
        if custom_role_id:
            from ._custom_roles import get_custom_role, get_role_permissions
            role_def = get_custom_role(custom_role_id)
            result = get_role_permissions(custom_role_id)
            if not role_def:
                from helpers._permissions import get_default_permissions
                base_defaults = get_default_permissions(role or "user")
                result = dict(result)
                result["enabled_permissions"] = set(base_defaults)
            if cache is not None:
                cache[user_id] = result
            return result

        # Standard permissions path
        # Check whether an admin has explicitly configured permissions for this
        # user by looking for rows in user_category_access.  Their presence is
        # the authoritative marker that the user is in "custom permissions mode"
        # and should have only what the DB records grant them (not role defaults).
        access_rows = cur.execute(
            "SELECT access_type, restricted FROM user_category_access WHERE user_id = ?",
            (user_id,)
        ).fetchall()

        if not access_rows:
            # No custom configuration ever recorded: fall back to role defaults.
            from helpers._permissions import get_default_permissions
            enabled = set(get_default_permissions(role or "user"))
            restricted_read = False
            allowed_read = []
            restricted_write = False
            allowed_write = []
        else:
            # User is in custom mode: use only what the DB grants.
            perm_rows = cur.execute(
                "SELECT permission_key FROM user_permissions WHERE user_id = ?",
                (user_id,)
            ).fetchall()
            enabled = set(r["permission_key"] for r in perm_rows)

            restricted_read = False
            restricted_write = False
            for row in access_rows:
                if row["access_type"] == "read":
                    restricted_read = bool(row["restricted"])
                elif row["access_type"] == "write":
                    restricted_write = bool(row["restricted"])

            cat_rows = cur.execute(
                "SELECT category_id, access_type FROM user_allowed_categories WHERE user_id = ?",
                (user_id,)
            ).fetchall()
            allowed_read = [r["category_id"] for r in cat_rows if r["access_type"] == "read"]
            allowed_write = [r["category_id"] for r in cat_rows if r["access_type"] == "write"]

    result = {
        'enabled_permissions': enabled,
        'category_access': {
            'restricted': restricted_read,
            'allowed_category_ids': allowed_read,
        },
        'category_write_access': {
            'restricted': restricted_write,
            'allowed_category_ids': allowed_write,
        }
    }
    # Store in the per-request cache so subsequent calls in the same request
    # (e.g. filtering many sidebar categories/pages) reuse this result.
    if cache is not None:
        cache[user_id] = result
    return result


def set_user_permissions(user_id, permission_keys,
                        read_restricted=False, read_category_ids=None,
                        write_restricted=False, write_category_ids=None):
    """Set custom permissions for a user.

    Args:
        user_id: User ID
        permission_keys: Set or list of permission keys to enable
        read_restricted: Whether to restrict read access to specific categories
        read_category_ids: List of category IDs for read access (if restricted)
        write_restricted: Whether to restrict write access to specific categories
        write_category_ids: List of category IDs for write access (if restricted)
    """
    with get_db_context() as conn:
        cur = conn.cursor()

        user_row = cur.execute(
            "SELECT role FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if not user_row:
            raise ValueError("User not found")
        role = user_row["role"]

        from helpers._permissions import sanitize_permission_keys

        permission_keys = sanitize_permission_keys(role, permission_keys)
        read_category_ids = list(dict.fromkeys(read_category_ids or []))
        write_category_ids = list(dict.fromkeys(write_category_ids or []))

        if role != "editor":
            write_restricted = False
            write_category_ids = []
        elif not write_restricted:
            read_restricted = False
            read_category_ids = []
        elif read_restricted:
            read_category_ids = list(dict.fromkeys(read_category_ids + write_category_ids))

        # Clear existing permissions
        cur.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_category_access WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (user_id,))

        # Insert new permissions
        for key in permission_keys:
            cur.execute(
                "INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                (user_id, key)
            )

        # Set read category access
        cur.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', ?)",
            (user_id, 1 if read_restricted else 0)
        )
        if read_restricted and read_category_ids:
            for cat_id in read_category_ids:
                cur.execute(
                    "INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'read')",
                    (user_id, cat_id)
                )

        # Set write category access
        cur.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', ?)",
            (user_id, 1 if write_restricted else 0)
        )
        if write_restricted and write_category_ids:
            for cat_id in write_category_ids:
                cur.execute(
                    "INSERT INTO user_allowed_categories (user_id, category_id, access_type) VALUES (?, ?, 'write')",
                    (user_id, cat_id)
                )

        conn.commit()
    # Invalidate the per-request cache so any subsequent permission checks in
    # this request (e.g. after an admin updates permissions) see the new values.
    cache = _get_permissions_cache()
    if cache is not None:
        cache.pop(user_id, None)

    # Auto-withdraw pending contributions if user was granted page.edit_all
    if "page.edit_all" in permission_keys and role == "user":
        from ._contributions import auto_withdraw_contributions_for_promoted_user
        auto_withdraw_contributions_for_promoted_user(
            user_id, reason="Granted page.edit_all permission: direct edit access"
        )


def has_permission(user, permission_key):
    """Check if a user has a specific permission.

    Checks plugin state: if the permission is gated by a plugin that
    is currently disabled, the permission is effectively revoked.

    Args:
        user: User dict with at least 'id' and 'role' keys
        permission_key: Permission key to check

    Returns:
        bool: True if user has the permission
    """
    if not user:
        return False

    # Check if the permission is gated by a disabled plugin
    from helpers._permissions import PERMISSION_PLUGIN_MAP
    plugin_id = PERMISSION_PLUGIN_MAP.get(permission_key)
    if plugin_id:
        from ._plugins import is_plugin_enabled, list_plugins
        # Fallback: if no plugins are registered in the DB yet (common in tests
        # before seeding), treat all built-ins as enabled.
        plugins = list_plugins()
        if not plugins:
            pass  # Assume enabled if database is unseeded
        elif not is_plugin_enabled(plugin_id):
            return False

    # Admins and owners have all permissions
    # Use subscript access for sqlite3.Row compatibility (no .get() method)
    user_keys = user.keys() if hasattr(user, "keys") else []
    role = user["role"] if "role" in user_keys else None
    if role in ("admin", "owner"):
        return True

    # Legacy chat_disabled override
    if permission_key.startswith("chat."):
        is_disabled = user["chat_disabled"] if "chat_disabled" in user_keys else False
        if is_disabled:
            return False

    from helpers._permissions import is_permission_assignable_to_role

    if not is_permission_assignable_to_role(permission_key, role):
        return False

    # Regular users and editors use custom permissions
    permissions = get_user_permissions(user["id"])
    return permission_key in permissions['enabled_permissions']


def has_category_read_access(user, category_id):
    """Check if user has read access to a specific category.

    Args:
        user: User dict
        category_id: Category ID to check (can be None for uncategorized)

    Returns:
        bool: True if user has access
    """
    if not user:
        return False

    # Admins always have access (handle both dict and sqlite3.Row)
    role = user["role"] if "role" in user.keys() else None
    if role in ("admin", "owner"):
        return True

    user_id = user["id"] if "id" in user.keys() else None
    if not user_id:
        return False

    permissions = get_user_permissions(user_id)
    cat_access = permissions['category_access']

    # If not restricted, user has access to all
    if not cat_access['restricted']:
        return True

    # If restricted, check if category is in allowed list
    # Note: uncategorized pages (None) are not accessible when restricted
    if category_id is None:
        return False

    if int(category_id) in cat_access['allowed_category_ids']:
        return True

    return has_category_write_access(user, category_id)


def has_category_write_access(user, category_id):
    """Check if user has write access to a specific category.

    Args:
        user: User dict
        category_id: Category ID to check (can be None for uncategorized)

    Returns:
        bool: True if user has write access
    """
    if not user:
        return False

    # Admins always have access (handle both dict and sqlite3.Row)
    role = user["role"] if "role" in user.keys() else None
    if role in ("admin", "owner"):
        return True
    if role != "editor":
        return False

    user_id = user["id"] if "id" in user.keys() else None
    if not user_id:
        return False

    perms = get_user_permissions(user_id)
    cat_access = perms['category_write_access']

    # If not restricted, user has write access to all
    if not cat_access['restricted']:
        return True

    # If restricted, check if category is in allowed list
    # Note: uncategorized pages (None) are not accessible when restricted
    if category_id is None:
        return False

    return int(category_id) in cat_access['allowed_category_ids']


def clear_user_permissions(user_id):
    """Clear all custom permissions for a user, reverting them to role defaults.

    Removes all explicitly granted permission keys, category access rows, and
    the custom-mode marker so that :func:`get_user_permissions` falls back to
    the user's role defaults on the next call.

    Args:
        user_id: User ID
    """
    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_category_access WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (user_id,))
        conn.commit()
