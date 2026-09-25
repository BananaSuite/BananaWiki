"""Custom role management: create, edit, delete, and assign named roles.

Each custom role is a reusable bundle of permissions and category access
settings.  When a role is assigned to a user, the user inherits all
permissions defined by that role.  Changing the role definition
automatically updates every assigned user's effective permissions.

When a role is deleted (or a user is removed from a role without a
substitute), the system finds the *most similar* surviving role and
reassigns affected users automatically.
"""

from ._connection import get_db_context, retry_on_busy


def create_custom_role(name, description="", base_role="user",
                       permission_keys=None, read_restricted=False,
                       read_category_ids=None, write_restricted=False,
                       write_category_ids=None, created_by=None):
    """Create a new custom role.

    Args:
        name: Unique human-readable role name.
        description: Optional description text.
        base_role: The base role tier ('user' or 'editor').  Determines
            which permission keys are valid for assignment.
        permission_keys: Iterable of permission key strings to enable.
        read_restricted: Whether read access is limited to specific categories.
        read_category_ids: Category IDs for read access (when restricted).
        write_restricted: Whether write access is limited to specific categories.
        write_category_ids: Category IDs for write access (when restricted).
        created_by: User ID of the admin who created the role.

    Returns:
        int: The new role's ID.
    """
    from helpers._permissions import sanitize_permission_keys

    permission_keys = set(permission_keys or [])
    permission_keys = sanitize_permission_keys(base_role, permission_keys)
    read_category_ids = list(dict.fromkeys(read_category_ids or []))
    write_category_ids = list(dict.fromkeys(write_category_ids or []))

    # Enforce: non-editors cannot have write restrictions
    if base_role != "editor":
        write_restricted = False
        write_category_ids = []
    elif write_restricted:
        # Coupling rule: write access always implies read access.
        read_restricted = True
        read_category_ids = list(dict.fromkeys(read_category_ids + write_category_ids))

    with get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO custom_roles
               (name, description, base_role, read_restricted, write_restricted, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name.strip(), description.strip(), base_role,
             1 if read_restricted else 0, 1 if write_restricted else 0,
             created_by),
        )
        role_id = cur.lastrowid

        # Permissions
        for key in permission_keys:
            cur.execute(
                "INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                (role_id, key),
            )

        # Category access
        for cat_id in read_category_ids:
            cur.execute(
                "INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, 'read')",
                (role_id, cat_id),
            )
        for cat_id in write_category_ids:
            cur.execute(
                "INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, 'write')",
                (role_id, cat_id),
            )

        conn.commit()
    return role_id


@retry_on_busy
def get_custom_role(role_id):
    """Fetch a custom role by ID.

    Returns:
        dict or None: {id, name, description, base_role, read_restricted,
        write_restricted, permission_keys, read_category_ids,
        write_category_ids, user_count, created_by, created_at, updated_at}
    """
    with get_db_context() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM custom_roles WHERE id = ?", (role_id,)
        ).fetchone()
        if not row:
            return None

        perm_rows = cur.execute(
            "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?",
            (role_id,),
        ).fetchall()
        permission_keys = {r[0] for r in perm_rows}

        read_cats = [
            r[0] for r in cur.execute(
                "SELECT category_id FROM custom_role_categories WHERE role_id = ? AND access_type = 'read'",
                (role_id,),
            ).fetchall()
        ]
        write_cats = [
            r[0] for r in cur.execute(
                "SELECT category_id FROM custom_role_categories WHERE role_id = ? AND access_type = 'write'",
                (role_id,),
            ).fetchall()
        ]

        user_count = cur.execute(
            "SELECT COUNT(*) FROM users WHERE custom_role_id = ?", (role_id,)
        ).fetchone()[0]

    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "base_role": row["base_role"],
        "read_restricted": bool(row["read_restricted"]),
        "write_restricted": bool(row["write_restricted"]),
        "permission_keys": permission_keys,
        "read_category_ids": read_cats,
        "write_category_ids": write_cats,
        "user_count": user_count,
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@retry_on_busy
def list_custom_roles():
    """Return all custom roles with user counts.

    Returns:
        list[dict]: Sorted by name.
    """
    with get_db_context() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            """SELECT cr.*,
                      (SELECT COUNT(*) FROM users u WHERE u.custom_role_id = cr.id) AS user_count
               FROM custom_roles cr ORDER BY cr.name""",
        ).fetchall()

    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "base_role": row["base_role"],
            "read_restricted": bool(row["read_restricted"]),
            "write_restricted": bool(row["write_restricted"]),
            "user_count": row["user_count"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return result


def update_custom_role(role_id, name=None, description=None, base_role=None,
                       permission_keys=None, read_restricted=None,
                       read_category_ids=None, write_restricted=None,
                       write_category_ids=None):
    """Update a custom role's definition.

    Only the fields provided (not ``None``) are changed.  When
    *permission_keys* is provided the full permission set is replaced.
    When *read_category_ids* or *write_category_ids* are provided the
    full category lists are replaced.

    After updating the role, all users assigned to this role will
    automatically have the new permissions (since permissions are
    resolved from the role at query time).
    """
    existing = get_custom_role(role_id)
    if not existing:
        raise ValueError("Custom role not found")

    effective_base = base_role if base_role is not None else existing["base_role"]
    effective_read_restricted = existing["read_restricted"] if read_restricted is None else bool(read_restricted)
    effective_write_restricted = existing["write_restricted"] if write_restricted is None else bool(write_restricted)
    effective_read_category_ids = (
        list(existing["read_category_ids"])
        if read_category_ids is None else list(dict.fromkeys(read_category_ids))
    )
    effective_write_category_ids = (
        list(existing["write_category_ids"])
        if write_category_ids is None else list(dict.fromkeys(write_category_ids))
    )

    force_clear_write_scope = False
    if effective_base != "editor":
        force_clear_write_scope = True
        effective_write_restricted = False
        effective_write_category_ids = []
    elif effective_write_restricted:
        # Coupling rule: write access always implies read access.
        effective_read_restricted = True
        effective_read_category_ids = list(
            dict.fromkeys(effective_read_category_ids + effective_write_category_ids)
        )

    with get_db_context() as conn:
        cur = conn.cursor()

        # Update scalar fields
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())
        if base_role is not None:
            updates.append("base_role = ?")
            params.append(base_role)
        if read_restricted is not None:
            updates.append("read_restricted = ?")
            params.append(1 if effective_read_restricted else 0)
        if write_restricted is not None or force_clear_write_scope:
            updates.append("write_restricted = ?")
            params.append(1 if effective_write_restricted else 0)

        if updates:
            updates.append("updated_at = datetime('now')")
            params.append(role_id)
            cur.execute(
                f"UPDATE custom_roles SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
                params,
            )

        # Replace permission keys
        if permission_keys is not None:
            from helpers._permissions import sanitize_permission_keys
            permission_keys = sanitize_permission_keys(effective_base, permission_keys)
            cur.execute("DELETE FROM custom_role_permissions WHERE role_id = ?", (role_id,))
            for key in permission_keys:
                cur.execute(
                    "INSERT INTO custom_role_permissions (role_id, permission_key) VALUES (?, ?)",
                    (role_id, key),
                )

        # Replace read category access
        if read_category_ids is not None:
            cur.execute(
                "DELETE FROM custom_role_categories WHERE role_id = ? AND access_type = 'read'",
                (role_id,),
            )
            for cat_id in effective_read_category_ids:
                cur.execute(
                    "INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, 'read')",
                    (role_id, cat_id),
                )

        # Replace write category access
        if write_category_ids is not None or force_clear_write_scope:
            cur.execute(
                "DELETE FROM custom_role_categories WHERE role_id = ? AND access_type = 'write'",
                (role_id,),
            )
            for cat_id in effective_write_category_ids:
                cur.execute(
                    "INSERT INTO custom_role_categories (role_id, category_id, access_type) VALUES (?, ?, 'write')",
                    (role_id, cat_id),
                )

        # If base_role changed, update all assigned users' role field
        if base_role is not None and base_role != existing["base_role"]:
            cur.execute(
                "UPDATE users SET role = ? WHERE custom_role_id = ?",
                (base_role, role_id),
            )

        conn.commit()

    # Auto-withdraw pending contributions if role upgrade grants direct edit access
    if base_role is not None and base_role in ("editor", "admin", "owner") and existing["base_role"] == "user":
        from ._contributions import auto_withdraw_contributions_for_promoted_user
        assigned_users = get_users_with_role(role_id)
        for u in assigned_users:
            auto_withdraw_contributions_for_promoted_user(u["id"])

    # Invalidate per-request permission caches
    _invalidate_permissions_cache()


def delete_custom_role(role_id, replacement_role_id=None):
    """Delete a custom role and reassign its users.

    Args:
        role_id: Role to delete.
        replacement_role_id: If given, move users to this role.
            If ``None``, the system finds the most similar surviving role.
            If no surviving roles exist, users get their base role's
            default permissions directly (custom_role_id set to NULL).
    """
    role = get_custom_role(role_id)
    if not role:
        raise ValueError("Custom role not found")

    with get_db_context() as conn:
        cur = conn.cursor()

        # Find users assigned to this role
        users = cur.execute(
            "SELECT id FROM users WHERE custom_role_id = ?", (role_id,)
        ).fetchall()
        user_ids = [u[0] for u in users]

        if user_ids:
            if replacement_role_id and replacement_role_id != role_id:
                # Verify replacement exists
                replacement = cur.execute(
                    "SELECT id, base_role FROM custom_roles WHERE id = ?",
                    (replacement_role_id,),
                ).fetchone()
                if replacement:
                    cur.execute(
                        "UPDATE users SET custom_role_id = ?, role = ? WHERE custom_role_id = ?",
                        (replacement_role_id, replacement["base_role"], role_id),
                    )
                else:
                    replacement_role_id = None

            if not replacement_role_id:
                # Find most similar role
                best = _find_most_similar_role(role_id, conn)
                if best:
                    best_row = cur.execute(
                        "SELECT id, base_role FROM custom_roles WHERE id = ?",
                        (best,),
                    ).fetchone()
                    if best_row:
                        cur.execute(
                            "UPDATE users SET custom_role_id = ?, role = ? WHERE custom_role_id = ?",
                            (best, best_row["base_role"], role_id),
                        )
                    else:
                        # Fallback: clear custom role
                        cur.execute(
                            "UPDATE users SET custom_role_id = NULL WHERE custom_role_id = ?",
                            (role_id,),
                        )
                        _set_default_permissions_for_users(user_ids, role["base_role"], cur)
                else:
                    # No other roles exist: clear custom role assignment
                    cur.execute(
                        "UPDATE users SET custom_role_id = NULL WHERE custom_role_id = ?",
                        (role_id,),
                    )
                    _set_default_permissions_for_users(user_ids, role["base_role"], cur)

        # Delete role and cascading data
        cur.execute("DELETE FROM custom_role_permissions WHERE role_id = ?", (role_id,))
        cur.execute("DELETE FROM custom_role_categories WHERE role_id = ?", (role_id,))
        cur.execute("DELETE FROM custom_roles WHERE id = ?", (role_id,))
        conn.commit()

    _invalidate_permissions_cache()


# Assigning a role rewrites the user's own ``role`` column to the role's
# ``base_role`` and drops their per-user permission rows, so a user with a
# custom role draws permissions from the role alone.

def assign_custom_role(user_id, role_id):
    """Assign a custom role to a user.

    This updates the user's base ``role`` column to match the custom
    role's ``base_role`` and sets ``custom_role_id``.  The user's old
    per-user permission rows are cleared since permissions now come
    from the role.
    """
    with get_db_context() as conn:
        cur = conn.cursor()
        role = cur.execute(
            "SELECT id, base_role FROM custom_roles WHERE id = ?", (role_id,)
        ).fetchone()
        if not role:
            raise ValueError("Custom role not found")

        user = cur.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise ValueError("User not found")

        old_role = user["role"]

        # Update user
        cur.execute(
            "UPDATE users SET custom_role_id = ?, role = ? WHERE id = ?",
            (role_id, role["base_role"], user_id),
        )

        # Clear per-user permissions (they're now role-driven)
        cur.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_category_access WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (user_id,))

        conn.commit()

    # Auto-withdraw pending contributions if user gained direct edit access
    if role["base_role"] in ("editor", "admin", "owner") and old_role == "user":
        from ._contributions import auto_withdraw_contributions_for_promoted_user
        auto_withdraw_contributions_for_promoted_user(user_id)

    _invalidate_permissions_cache()


def unassign_custom_role(user_id, replacement_role_id=None):
    """Remove a user from their custom role.

    If *replacement_role_id* is given, assign that role instead.
    Otherwise, find the most similar role.  If no roles exist,
    set default permissions for the user's current base role.
    """
    with get_db_context() as conn:
        cur = conn.cursor()
        user = cur.execute(
            "SELECT id, role, custom_role_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            raise ValueError("User not found")

        old_role_id = user["custom_role_id"]

        if replacement_role_id:
            replacement = cur.execute(
                "SELECT id, base_role FROM custom_roles WHERE id = ?",
                (replacement_role_id,),
            ).fetchone()
            if replacement:
                cur.execute(
                    "UPDATE users SET custom_role_id = ?, role = ? WHERE id = ?",
                    (replacement_role_id, replacement["base_role"], user_id),
                )
                conn.commit()
                _invalidate_permissions_cache()
                return

        # Try most similar role
        if old_role_id:
            best = _find_most_similar_role(old_role_id, conn)
            if best:
                best_row = cur.execute(
                    "SELECT id, base_role FROM custom_roles WHERE id = ?", (best,)
                ).fetchone()
                if best_row:
                    cur.execute(
                        "UPDATE users SET custom_role_id = ?, role = ? WHERE id = ?",
                        (best, best_row["base_role"], user_id),
                    )
                    conn.commit()
                    _invalidate_permissions_cache()
                    return

        # Fallback: no roles available, clear and set defaults
        base = user["role"] or "user"
        cur.execute("UPDATE users SET custom_role_id = NULL WHERE id = ?", (user_id,))
        _set_default_permissions_for_users([user_id], base, cur)
        conn.commit()

    _invalidate_permissions_cache()


@retry_on_busy
def get_users_with_role(role_id):
    """Return list of users assigned to a custom role."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT id, username, role FROM users WHERE custom_role_id = ? ORDER BY username",
            (role_id,),
        ).fetchall()
    return [{"id": r["id"], "username": r["username"], "role": r["role"]} for r in rows]


@retry_on_busy
def get_user_custom_role(user_id):
    """Return the custom role dict for a user, or None if not assigned."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT custom_role_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row or not row["custom_role_id"]:
        return None
    return get_custom_role(row["custom_role_id"])


def get_role_permissions(role_id):
    """Get the effective permissions for a custom role.

    Returns the same dict shape as ``get_user_permissions()`` for
    compatibility, but sourced from the role definition.
    """
    role = get_custom_role(role_id)
    if not role:
        return {
            "enabled_permissions": set(),
            "category_access": {"restricted": False, "allowed_category_ids": []},
            "category_write_access": {"restricted": False, "allowed_category_ids": []},
        }

    read_ids = list(role["read_category_ids"])
    read_restricted = bool(role["read_restricted"])
    if role["write_restricted"]:
        read_restricted = True
        read_ids = list(dict.fromkeys(read_ids + list(role["write_category_ids"])))

    return {
        "enabled_permissions": role["permission_keys"],
        "category_access": {
            "restricted": read_restricted,
            "allowed_category_ids": read_ids,
        },
        "category_write_access": {
            "restricted": role["write_restricted"],
            "allowed_category_ids": role["write_category_ids"],
        },
    }


# When a role is deleted its holders must land somewhere; the scoring below
# picks the closest surviving role by Jaccard overlap of permission keys.

@retry_on_busy
def _find_most_similar_role(role_id, conn=None):
    """Find the custom role most similar to *role_id*.

    Similarity is measured by the Jaccard index of permission keys
    plus a bonus for matching base_role.  Returns the ID of the best
    match, or ``None`` if no other roles exist.
    """
    def _do(conn):
        """Run the similarity query within the given connection."""
        cur = conn.cursor()
        target = cur.execute(
            "SELECT id, base_role FROM custom_roles WHERE id = ?", (role_id,)
        ).fetchone()
        if not target:
            return None

        target_perms = {
            r[0] for r in cur.execute(
                "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?",
                (role_id,),
            ).fetchall()
        }

        others = cur.execute(
            "SELECT id, base_role FROM custom_roles WHERE id != ?", (role_id,)
        ).fetchall()
        if not others:
            return None

        best_id = None
        best_score = -1.0

        for other in others:
            other_perms = {
                r[0] for r in cur.execute(
                    "SELECT permission_key FROM custom_role_permissions WHERE role_id = ?",
                    (other["id"],),
                ).fetchall()
            }

            # Jaccard similarity
            union = target_perms | other_perms
            if union:
                jaccard = len(target_perms & other_perms) / len(union)
            else:
                jaccard = 1.0  # both empty → identical

            # Bonus for matching base_role
            score = jaccard + (0.2 if other["base_role"] == target["base_role"] else 0.0)

            if score > best_score:
                best_score = score
                best_id = other["id"]

        return best_id

    if conn is not None:
        return _do(conn)

    with get_db_context() as conn:
        return _do(conn)


def _set_default_permissions_for_users(user_ids, base_role, cur):
    """Set per-user default permissions when custom role assignment is cleared."""
    from helpers._permissions import get_default_permissions

    defaults = get_default_permissions(base_role)
    for uid in user_ids:
        # Clear existing per-user permissions
        cur.execute("DELETE FROM user_permissions WHERE user_id = ?", (uid,))
        cur.execute("DELETE FROM user_category_access WHERE user_id = ?", (uid,))
        cur.execute("DELETE FROM user_allowed_categories WHERE user_id = ?", (uid,))

        # Insert default permissions
        for key in defaults:
            cur.execute(
                "INSERT INTO user_permissions (user_id, permission_key) VALUES (?, ?)",
                (uid, key),
            )
        # Set unrestricted access
        cur.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'read', 0)",
            (uid,),
        )
        cur.execute(
            "INSERT INTO user_category_access (user_id, access_type, restricted) VALUES (?, 'write', 0)",
            (uid,),
        )


def _invalidate_permissions_cache():
    """Invalidate the per-request permission cache if in a Flask context."""
    try:
        from flask import g
        g._permissions_cache = {}
    except RuntimeError:
        pass
