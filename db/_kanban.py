"""Kanban board CRUD operations."""

import json
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def create_board(title, description, created_by):
    """Create a new kanban board. Returns the new board id."""
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO kanban_boards (title, description, created_by) VALUES (?, ?, ?)",
            (title, description, created_by),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_board(board_id):
    """Return a single board row or None."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM kanban_boards WHERE id = ?", (board_id,)).fetchone()
    return row


@retry_on_busy
def list_boards():
    """Return all boards ordered by creation date desc."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_boards ORDER BY created_at DESC"
        ).fetchall()
    return rows


def update_board(board_id, **kwargs):
    """Update board fields. Title, description and creator are allowed."""
    allowed = {"title", "description", "created_by"}
    for k in kwargs:
        if k not in allowed:
            raise ValueError(f"Invalid column: {k}")
    with get_db_context() as conn:
        set_parts = []
        vals = []
        for col in allowed:
            if col in kwargs:
                set_parts.append(f"{col}=?")
                vals.append(kwargs[col])
        if set_parts:
            vals.append(board_id)
            conn.execute(
                f"UPDATE kanban_boards SET {', '.join(set_parts)} WHERE id=?",
                vals,
            )
            conn.commit()


@retry_on_busy
def get_user_board_order(user_id):
    """Return a list of board_ids in user's preferred order (or global when
    *user_id* is None).  Boards that no longer exist are silently omitted."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT board_id FROM kanban_user_board_order "
            "WHERE user_id IS ? "
            "ORDER BY sort_order ASC",
            (user_id,),
        ).fetchall()
    return [r["board_id"] for r in rows]


@retry_on_busy
def save_user_board_order(user_id, board_ids):
    """Replace the ordering for *user_id* (or global order when *user_id* is
    None) with the given list of board_ids."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_user_board_order WHERE user_id IS ?",
            (user_id,),
        )
        for i, bid in enumerate(board_ids):
            conn.execute(
                "INSERT OR IGNORE INTO kanban_user_board_order "
                "(user_id, board_id, sort_order) VALUES (?, ?, ?)",
                (user_id, bid, i),
            )
        conn.commit()


def delete_board(board_id):
    """Delete a board and all its columns/tickets (cascade).

    Returns a list of attachment filenames that were stored on disk so the
    caller can remove the physical files after the database transaction.
    """
    with get_db_context() as conn:
        # Collect attachment filenames before cascade deletes them.
        rows = conn.execute(
            "SELECT ka.filename FROM kanban_ticket_attachments ka "
            "JOIN kanban_tickets kt ON ka.ticket_id = kt.id "
            "JOIN kanban_columns kc ON kt.column_id = kc.id "
            "WHERE kc.board_id = ?",
            (board_id,),
        ).fetchall()
        files = [r["filename"] for r in rows]
        conn.execute("DELETE FROM kanban_boards WHERE id = ?", (board_id,))
        conn.commit()
    return files


def create_column(board_id, title, sort_order=0):
    """Create a new column in a board. Returns the new column id."""
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO kanban_columns (board_id, title, sort_order) VALUES (?, ?, ?)",
            (board_id, title, sort_order),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_column(column_id):
    """Return a single column row or None."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM kanban_columns WHERE id = ?", (column_id,)).fetchone()
    return row


@retry_on_busy
def list_columns(board_id):
    """Return all columns for a board, ordered by sort_order."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_columns WHERE board_id = ? ORDER BY sort_order, id",
            (board_id,),
        ).fetchall()
    return rows


def update_column(column_id, **kwargs):
    """Update column fields (title, sort_order)."""
    allowed = {"title", "sort_order"}
    for k in kwargs:
        if k not in allowed:
            raise ValueError(f"Invalid column: {k}")
    with get_db_context() as conn:
        set_parts = []
        vals = []
        for col in allowed:
            if col in kwargs:
                set_parts.append(f"{col}=?")
                vals.append(kwargs[col])
        if set_parts:
            vals.append(column_id)
            conn.execute(
                f"UPDATE kanban_columns SET {', '.join(set_parts)} WHERE id=?",
                vals,
            )
            conn.commit()


def update_columns_sort_order(board_id, column_order):
    """Bulk-update sort order of columns. column_order is a list of column IDs in desired order."""
    with get_db_context() as conn:
        for idx, col_id in enumerate(column_order):
            conn.execute(
                "UPDATE kanban_columns SET sort_order = ? WHERE id = ? AND board_id = ?",
                (idx, col_id, board_id),
            )
        conn.commit()


def delete_column(column_id):
    """Delete a column and all its tickets (cascade).

    Returns a list of attachment filenames that were stored on disk so the
    caller can remove the physical files after the database transaction.
    """
    with get_db_context() as conn:
        # Collect attachment filenames before cascade deletes them.
        rows = conn.execute(
            "SELECT ka.filename FROM kanban_ticket_attachments ka "
            "JOIN kanban_tickets kt ON ka.ticket_id = kt.id "
            "WHERE kt.column_id = ?",
            (column_id,),
        ).fetchall()
        files = [r["filename"] for r in rows]
        conn.execute("DELETE FROM kanban_columns WHERE id = ?", (column_id,))
        conn.commit()
    return files


def create_ticket(column_id, title, description, created_by, priority="medium", sort_order=0):
    """Create a new ticket in a column. Returns the new ticket id."""
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO kanban_tickets (column_id, title, description, created_by, priority, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (column_id, title, description, created_by, priority, sort_order),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_ticket(ticket_id):
    """Return a single ticket row or None."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM kanban_ticket_attachments WHERE ticket_id = t.id) AS attachment_count "
            "FROM kanban_tickets t WHERE t.id = ?",
            (ticket_id,)
        ).fetchone()
    return row


@retry_on_busy
def list_tickets(column_id):
    """Return all tickets for a column, ordered by sort_order."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT t.*, u.username AS assigned_to_username, "
            "(SELECT COUNT(*) FROM kanban_ticket_attachments WHERE ticket_id = t.id) AS attachment_count "
            "FROM kanban_tickets t "
            "LEFT JOIN users u ON t.assigned_to = u.id "
            "WHERE t.column_id = ? ORDER BY t.sort_order, t.id",
            (column_id,),
        ).fetchall()
    return rows


@retry_on_busy
def list_board_tickets(board_id):
    """Return all tickets for a board (across all columns), with column info."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT t.*, c.title AS column_title, c.sort_order AS column_sort_order, "
            "(SELECT COUNT(*) FROM kanban_ticket_attachments WHERE ticket_id = t.id) AS attachment_count "
            "FROM kanban_tickets t "
            "JOIN kanban_columns c ON t.column_id = c.id "
            "WHERE c.board_id = ? "
            "ORDER BY c.sort_order, t.sort_order, t.id",
            (board_id,),
        ).fetchall()
    return rows


def update_ticket(ticket_id, **kwargs):
    """Update ticket fields."""
    allowed = {"title", "description", "priority", "column_id", "sort_order", "assigned_to", "due_date", "color", "labels"}
    for k in kwargs:
        if k not in allowed:
            raise ValueError(f"Invalid column: {k}")
    with get_db_context() as conn:
        set_parts = []
        vals = []
        for col in allowed:
            if col in kwargs:
                set_parts.append(f"{col}=?")
                vals.append(kwargs[col])
        if set_parts:
            vals.append(ticket_id)
            conn.execute(
                f"UPDATE kanban_tickets SET {', '.join(set_parts)} WHERE id=?",
                vals,
            )
            conn.commit()


def move_ticket(ticket_id, target_column_id, sort_order):
    """Move a ticket to a different column and/or position."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE kanban_tickets SET column_id = ?, sort_order = ? WHERE id = ?",
            (target_column_id, sort_order, ticket_id),
        )
        conn.commit()


def update_tickets_sort_order(column_id, ticket_order):
    """Bulk-update sort order of tickets within a column. ticket_order is list of ticket IDs."""
    with get_db_context() as conn:
        for idx, tid in enumerate(ticket_order):
            # Only update tickets that already belong to the target column.
            conn.execute(
                "UPDATE kanban_tickets SET sort_order = ? "
                "WHERE id = ? AND column_id = ?",
                (idx, tid, column_id),
            )
        conn.commit()


def delete_ticket(ticket_id):
    """Delete a ticket.

    Returns a list of attachment filenames that were stored on disk so the
    caller can remove the physical files after the database transaction.
    """
    with get_db_context() as conn:
        # Collect attachment filenames before cascade deletes them.
        rows = conn.execute(
            "SELECT filename FROM kanban_ticket_attachments WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchall()
        files = [r["filename"] for r in rows]
        conn.execute("DELETE FROM kanban_tickets WHERE id = ?", (ticket_id,))
        conn.commit()
    return files


@retry_on_busy
def count_board_tickets(board_id):
    """Count total tickets across all columns of a board."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM kanban_tickets t "
            "JOIN kanban_columns c ON t.column_id = c.id "
            "WHERE c.board_id = ?",
            (board_id,),
        ).fetchone()
    return row["cnt"] if row else 0


# Visibility ('private', 'shared', 'public') and the per-user share rows below
# are evaluated together: a share row only ever widens access, never narrows
# what the board's own visibility already allows.


def set_board_visibility(board_id, visibility):
    """Set board visibility: 'private', 'shared', or 'public'."""
    if visibility not in ("private", "shared", "public"):
        raise ValueError(f"Invalid visibility: {visibility}")
    with get_db_context() as conn:
        conn.execute(
            "UPDATE kanban_boards SET visibility = ? WHERE id = ?",
            (visibility, board_id),
        )
        conn.commit()


def set_board_share(board_id, share_type, target, access_level="view"):
    """Add or update a share rule for a board.

    share_type: 'role' or 'user'
    target: role name ('admin','editor','user') or user_id
    access_level: 'view' or 'write'
    """
    if share_type not in ("role", "user"):
        raise ValueError(f"Invalid share_type: {share_type}")
    if access_level not in ("view", "write"):
        raise ValueError(f"Invalid access_level: {access_level}")
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO kanban_board_shares (board_id, share_type, target, access_level) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(board_id, share_type, target) DO UPDATE SET access_level = ?",
            (board_id, share_type, target, access_level, access_level),
        )
        conn.commit()


def remove_board_share(board_id, share_type, target):
    """Remove a share rule for a board."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_board_shares WHERE board_id = ? AND share_type = ? AND target = ?",
            (board_id, share_type, target),
        )
        conn.commit()


@retry_on_busy
def get_board_shares(board_id):
    """Return all share rules for a board."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_board_shares WHERE board_id = ? ORDER BY share_type, target",
            (board_id,),
        ).fetchall()
    return rows


def clear_board_shares(board_id):
    """Remove all share rules for a board."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM kanban_board_shares WHERE board_id = ?", (board_id,))
        conn.commit()


@retry_on_busy
def user_can_view_board(user, board):
    """Check the board-level rules for a user who has global kanban access.

    Rules:
    - Admins/owners can always view all boards
    - Board creator can always view their own board
    - If board visibility is 'public', anyone with global kanban access can view
    - If board visibility is 'private' or 'shared', check explicit shares:
      - Role-based share with user's role and access_level in ('view', 'write')
      - User-specific share with access_level in ('view', 'write')

    This function does not check global kanban access or the kanban.view
    permission, and returns True for every ``public`` board.  Route code
    must call ``routes.kanban.user_can_view_kanban_board`` instead, which
    adds those checks and falls back to ``user_can_view_board_individual``
    for users without global access.
    """
    if not user or not board:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if board["created_by"] == user["id"]:
        return True
    visibility = board["visibility"] if "visibility" in board.keys() else "private"
    if visibility == "public":
        return True
    # Check explicit shares (role-based or user-specific)
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM kanban_board_shares "
            "WHERE board_id = ? AND ("
            "  (share_type = 'role' AND target = ?) OR "
            "  (share_type = 'user' AND target = ?)"
            ")",
            (board["id"], role, user["id"]),
        ).fetchone()
    return row is not None


@retry_on_busy
def user_can_write_board(user, board):
    """Check if a user has write access to a specific board.

    Rules:
    - Admins/owners can always write
    - Board creator can always write
    - Check explicit shares with access_level = 'write':
      - Role-based share
      - User-specific share
    """
    if not user or not board:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if board["created_by"] == user["id"]:
        return True
    # Check explicit write shares (role-based or user-specific)
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM kanban_board_shares "
            "WHERE board_id = ? AND access_level = 'write' AND ("
            "  (share_type = 'role' AND target = ?) OR "
            "  (share_type = 'user' AND target = ?)"
            ")",
            (board["id"], role, user["id"]),
        ).fetchone()
    return row is not None


@retry_on_busy
def list_boards_for_user(user):
    """Return all boards the user can view.

    Each row includes a ``creator_username`` column from a JOIN on the
    ``users`` table so callers can display the board owner without extra
    queries.

    - Admins see all boards
    - Others see: boards they own + public boards + boards shared with them (by role or user)
    """
    if not user:
        return []
    role = user["role"]
    user_id = user["id"]
    with get_db_context() as conn:
        if role in ("admin", "owner"):
            rows = conn.execute(
                "SELECT b.*, COALESCE(u.username, 'Unknown') AS creator_username "
                "FROM kanban_boards b "
                "LEFT JOIN users u ON b.created_by = u.id "
                "ORDER BY b.created_at DESC"
            ).fetchall()
            return rows
        rows = conn.execute(
            "SELECT DISTINCT b.*, COALESCE(u.username, 'Unknown') AS creator_username "
            "FROM kanban_boards b "
            "LEFT JOIN users u ON b.created_by = u.id "
            "LEFT JOIN kanban_board_shares s ON b.id = s.board_id "
            "WHERE b.created_by = ? "
            "   OR b.visibility = 'public' "
            "   OR (s.share_type = 'role' AND s.target = ?) "
            "   OR (s.share_type = 'user' AND s.target = ?) "
            "ORDER BY b.created_at DESC",
            (user_id, role, user_id),
        ).fetchall()
        return rows


@retry_on_busy
def list_public_boards():
    """Return boards visible to anonymous public-mode visitors."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT b.*, COALESCE(u.username, 'Unknown') AS creator_username "
            "FROM kanban_boards b "
            "LEFT JOIN users u ON b.created_by = u.id "
            "WHERE b.visibility = 'public' "
            "ORDER BY b.created_at DESC"
        ).fetchall()


@retry_on_busy
def list_boards_for_individual_user(user_id):
    """Return boards accessible to a user via explicit user-specific shares only.

    Each row includes a ``creator_username`` column from a JOIN on the
    ``users`` table.

    Used for users who do not have global kanban access but have been individually
    invited to specific boards.
    """
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT DISTINCT b.*, COALESCE(u.username, 'Unknown') AS creator_username "
            "FROM kanban_boards b "
            "LEFT JOIN users u ON b.created_by = u.id "
            "JOIN kanban_board_shares s ON b.id = s.board_id "
            "WHERE s.share_type = 'user' AND s.target = ? "
            "ORDER BY b.created_at DESC",
            (user_id,),
        ).fetchall()
    return rows


@retry_on_busy
def user_has_any_share(user_id):
    """Return True if the user has at least one user-specific board share.

    Used to allow individually invited users through the global kanban access gate.
    """
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM kanban_board_shares WHERE share_type = 'user' AND target = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


@retry_on_busy
def user_can_view_board_individual(user, board):
    """Check if a user can view a board via an explicit user-specific share only.

    Used for users without global kanban access. They may only access boards
    where they have been individually invited.
    """
    if not user or not board:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if board["created_by"] == user["id"]:
        return True
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT id FROM kanban_board_shares "
            "WHERE board_id = ? AND share_type = 'user' AND target = ?",
            (board["id"], user["id"]),
        ).fetchone()
    return row is not None


def revoke_role_shares_for_restricted_roles(kanban_access):
    """Remove role-based board shares for roles that no longer have global kanban access.

    Called whenever the site-wide ``kanban_access`` setting is changed to ensure
    that existing board shares are consistent with the new policy.
    """
    with get_db_context() as conn:
        if kanban_access == "admin":
            # Only admins have global access: remove editor and user role shares
            conn.execute(
                "DELETE FROM kanban_board_shares "
                "WHERE share_type = 'role' AND target IN ('editor', 'user')"
            )
        elif kanban_access == "editor":
            # Admins and editors have global access: remove user role shares
            conn.execute(
                "DELETE FROM kanban_board_shares "
                "WHERE share_type = 'role' AND target = 'user'"
            )
        # If "all", no role shares need to be revoked
        conn.commit()


@retry_on_busy
def get_board_accessible_users(board_id, kanban_access):
    """Return a list of user rows who have access to the given board.

    Used to populate the assignee dropdown so that only users who can actually
    interact with the board appear as options.

    Inclusion rules (in order):
    1. Admins / owners always have access.
    2. The board creator always has access.
    3. Users with an explicit user-specific share on this board.
    4. Users whose role has a role-based share on this board AND whose role has
       global kanban access under the current ``kanban_access`` setting.
    5. If the board is public, all users who have global kanban access.
    """
    with get_db_context() as conn:
        board = conn.execute(
            "SELECT * FROM kanban_boards WHERE id = ?", (board_id,)
        ).fetchone()
        if not board:
            return []

        visibility = board["visibility"] if "visibility" in board.keys() else "private"
        creator_id = board["created_by"]

        # Collect explicit shares for this board
        share_rows = conn.execute(
            "SELECT share_type, target FROM kanban_board_shares WHERE board_id = ?",
            (board_id,),
        ).fetchall()
        shared_roles = {s["target"] for s in share_rows if s["share_type"] == "role"}
        shared_user_ids = {s["target"] for s in share_rows if s["share_type"] == "user"}

        # Determine which roles have global kanban access
        globally_accessible_roles = {"admin", "owner"}
        if kanban_access == "editor":
            globally_accessible_roles.add("editor")
        elif kanban_access == "all":
            globally_accessible_roles.update({"editor", "user"})

        all_users = conn.execute("SELECT * FROM users").fetchall()

    accessible = []
    seen_ids = set()
    for u in all_users:
        uid = u["id"]
        if uid in seen_ids:
            continue
        role = u["role"]
        if role in ("admin", "owner"):
            accessible.append(u)
            seen_ids.add(uid)
        elif uid == creator_id:
            accessible.append(u)
            seen_ids.add(uid)
        elif uid in shared_user_ids:
            accessible.append(u)
            seen_ids.add(uid)
        elif role in shared_roles and role in globally_accessible_roles:
            accessible.append(u)
            seen_ids.add(uid)
        elif visibility == "public" and role in globally_accessible_roles:
            accessible.append(u)
            seen_ids.add(uid)
    return accessible


def remove_assignees_without_board_access(board_id, kanban_access):
    """Clear the ``assigned_to`` field on any ticket whose assignee no longer has
    access to the board.

    Called after board sharing settings are updated to keep assignees consistent.
    """
    accessible_users = get_board_accessible_users(board_id, kanban_access)
    accessible_ids = {u["id"] for u in accessible_users}

    with get_db_context() as conn:
        # Drop multi-assignee rows for users who lost access
        if accessible_ids:
            placeholders = ",".join("?" * len(accessible_ids))
            conn.execute(
                "DELETE FROM kanban_ticket_assignees "
                "WHERE ticket_id IN ("
                "  SELECT t.id FROM kanban_tickets t "
                "  JOIN kanban_columns c ON t.column_id = c.id "
                "  WHERE c.board_id = ?"
                ") AND user_id NOT IN ("
                "  SELECT id FROM users WHERE id IN ({})"
                ")".format(placeholders),
                (board_id, *accessible_ids),
            )
        # Get all tickets for this board that have a primary assignee
        rows = conn.execute(
            "SELECT t.id, t.assigned_to FROM kanban_tickets t "
            "JOIN kanban_columns c ON t.column_id = c.id "
            "WHERE c.board_id = ? AND t.assigned_to IS NOT NULL AND t.assigned_to != ''",
            (board_id,),
        ).fetchall()
        for row in rows:
            if row["assigned_to"] not in accessible_ids:
                # Pick the next remaining accessible assignee (if any) as the
                # new primary so the legacy column stays in sync.
                next_row = conn.execute(
                    "SELECT user_id FROM kanban_ticket_assignees "
                    "WHERE ticket_id = ? ORDER BY assigned_at ASC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                new_primary = next_row["user_id"] if next_row else None
                conn.execute(
                    "UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?",
                    (new_primary, row["id"]),
                )
        conn.commit()


def remove_all_invalid_assignees(kanban_access):
    """Clear ``assigned_to`` on tickets across ALL boards where the assignee no
    longer has access.

    Called after the global ``kanban_access`` setting changes to keep assignees
    consistent across every board.
    """
    with get_db_context() as conn:
        board_ids = [
            r["id"] for r in conn.execute("SELECT id FROM kanban_boards").fetchall()
        ]
    for bid in board_ids:
        remove_assignees_without_board_access(bid, kanban_access)


@retry_on_busy
def user_has_board_access(user_id, board_id, kanban_access=None):
    """Return True if the given user has access to the specified board.

    Used for assignee validation when creating or updating tickets.

    Checks both board-level visibility/shares **and** global kanban access
    so that users who cannot actually reach the board are not considered
    valid assignees.

    The *kanban_access* parameter should be the current global
    ``kanban_access`` site setting value (e.g. ``"admin"``, ``"editor"``,
    ``"all"``).  When provided, users whose role falls outside the global
    access level are rejected unless they have an explicit user-specific
    share or are the board creator.
    """
    with get_db_context() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        board = conn.execute(
            "SELECT * FROM kanban_boards WHERE id = ?", (board_id,)
        ).fetchone()
    if not user or not board:
        return False
    role = user["role"]
    # Admins / owners always have access
    if role in ("admin", "owner"):
        return True
    # Board creator always has access
    if board["created_by"] == user_id:
        return True
    # Check for explicit user-specific share (bypasses global access gate)
    with get_db_context() as conn:
        user_share = conn.execute(
            "SELECT id FROM kanban_board_shares "
            "WHERE board_id = ? AND share_type = 'user' AND target = ?",
            (board_id, user_id),
        ).fetchone()
    if user_share:
        return True
    # For non-individually-invited users, they must have global kanban access
    if kanban_access:
        has_global = (
            kanban_access == "all"
            or (kanban_access == "editor" and role == "editor")
        )
        if not has_global:
            return False
    # Check board-level visibility and role shares
    return user_can_view_board(user, board)


# Tickets take multiple assignees, stored in ``kanban_ticket_assignees``.
# ``kanban_tickets.assigned_to`` is kept in sync as the "primary" assignee
# (first one assigned) so older code paths and the existing single-assignee
# filters keep working unchanged.

@retry_on_busy
def list_ticket_assignees(ticket_id):
    """Return all assignees for a ticket as user rows (id, username, role).
    Includes ``assigned_at`` so callers can render the list in stable order.
    """
    with get_db_context() as conn:
        return conn.execute(
            "SELECT u.id, u.username, u.role, a.assigned_at "
            "FROM kanban_ticket_assignees a "
            "JOIN users u ON a.user_id = u.id "
            "WHERE a.ticket_id = ? "
            "ORDER BY a.assigned_at ASC, u.username ASC",
            (ticket_id,),
        ).fetchall()


def list_ticket_assignee_ids(ticket_id):
    """Return just the user IDs for a ticket's assignees (in stable order)."""
    return [r["id"] for r in list_ticket_assignees(ticket_id)]


def set_ticket_assignees(ticket_id, user_ids):
    """Replace a ticket's assignee set with the provided list of user IDs.
    Also synchronises the legacy ``kanban_tickets.assigned_to`` column to the
    first ID in the list (or ``NULL`` if empty) so back-compat paths keep
    working.
    """
    cleaned = []
    seen = set()
    for uid in (user_ids or []):
        if not uid or uid in seen:
            continue
        seen.add(uid)
        cleaned.append(uid)
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_ticket_assignees WHERE ticket_id = ?",
            (ticket_id,),
        )
        for uid in cleaned:
            conn.execute(
                "INSERT OR IGNORE INTO kanban_ticket_assignees (ticket_id, user_id) "
                "VALUES (?, ?)",
                (ticket_id, uid),
            )
        primary = cleaned[0] if cleaned else None
        conn.execute(
            "UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?",
            (primary, ticket_id),
        )
        conn.commit()
    return cleaned


def add_ticket_assignee(ticket_id, user_id):
    """Add a single user as an assignee.  No-op if already assigned.
    If the ticket has no primary assignee yet, sets ``assigned_to`` to
    ``user_id`` to keep legacy paths consistent.
    """
    if not user_id:
        return
    with get_db_context() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO kanban_ticket_assignees (ticket_id, user_id) "
            "VALUES (?, ?)",
            (ticket_id, user_id),
        )
        row = conn.execute(
            "SELECT assigned_to FROM kanban_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        if row and not row["assigned_to"]:
            conn.execute(
                "UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?",
                (user_id, ticket_id),
            )
        conn.commit()


def remove_ticket_assignee(ticket_id, user_id):
    """Remove a single assignee from a ticket.  If the removed user was the
    primary assignee, promote the next remaining assignee (or NULL).
    """
    if not user_id:
        return
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_ticket_assignees WHERE ticket_id = ? AND user_id = ?",
            (ticket_id, user_id),
        )
        row = conn.execute(
            "SELECT assigned_to FROM kanban_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        if row and row["assigned_to"] == user_id:
            next_row = conn.execute(
                "SELECT user_id FROM kanban_ticket_assignees "
                "WHERE ticket_id = ? ORDER BY assigned_at ASC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            new_primary = next_row["user_id"] if next_row else None
            conn.execute(
                "UPDATE kanban_tickets SET assigned_to = ? WHERE id = ?",
                (new_primary, ticket_id),
            )
        conn.commit()


def add_ticket_attachment(ticket_id, filename, original_name, file_size, user_id, blob_id=None):
    """Record a new attachment for a kanban ticket. Returns the attachment id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO kanban_ticket_attachments "
            "(ticket_id, filename, original_name, file_size, uploaded_by, uploaded_at, blob_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, filename, original_name, file_size, user_id, now, blob_id),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_ticket_attachments(ticket_id):
    """Return all attachments for a ticket, ordered oldest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT a.*, COALESCE(u.username, 'deleted user') AS uploader_name "
            "FROM kanban_ticket_attachments a "
            "LEFT JOIN users u ON a.uploaded_by = u.id "
            "WHERE a.ticket_id = ? ORDER BY a.uploaded_at ASC",
            (ticket_id,),
        ).fetchall()


@retry_on_busy
def get_ticket_attachment(attachment_id):
    """Return a single kanban ticket attachment row by id."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM kanban_ticket_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()


def delete_ticket_attachment(attachment_id):
    """Delete a kanban ticket attachment record and its associated blob."""
    with get_db_context() as conn:
        row = conn.execute("SELECT blob_id FROM kanban_ticket_attachments WHERE id = ?", (attachment_id,)).fetchone()
        blob_id = row["blob_id"] if row else None
        conn.execute("DELETE FROM kanban_ticket_attachments WHERE id = ?", (attachment_id,))
        if blob_id:
            conn.execute("DELETE FROM file_blobs WHERE id=?", (blob_id,))
        conn.commit()


def add_ticket_history_entry(ticket_id, old_description, new_description, changed_by):
    """Record a description change for a ticket. Returns the entry id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO kanban_ticket_history "
            "(ticket_id, old_description, new_description, changed_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, old_description or "", new_description or "", changed_by, now),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_ticket_history(ticket_id):
    """Return all history entries for a ticket, newest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT h.*, COALESCE(u.username, 'deleted user') AS editor_name "
            "FROM kanban_ticket_history h "
            "LEFT JOIN users u ON h.changed_by = u.id "
            "WHERE h.ticket_id = ? ORDER BY h.created_at DESC",
            (ticket_id,),
        ).fetchall()


@retry_on_busy
def get_ticket_history_entry(entry_id):
    """Return a single ticket history entry by id."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT h.*, COALESCE(u.username, 'deleted user') AS editor_name "
            "FROM kanban_ticket_history h "
            "LEFT JOIN users u ON h.changed_by = u.id "
            "WHERE h.id = ?",
            (entry_id,),
        ).fetchone()


def add_ticket_comment(ticket_id, user_id, content):
    """Add a comment to a ticket. Returns the new comment id."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO kanban_ticket_comments "
            "(ticket_id, user_id, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, user_id, content, now, now),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_ticket_comments(ticket_id):
    """Return all comments for a ticket, oldest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT c.*, COALESCE(u.username, 'deleted user') AS author_name "
            "FROM kanban_ticket_comments c "
            "LEFT JOIN users u ON c.user_id = u.id "
            "WHERE c.ticket_id = ? ORDER BY c.created_at ASC",
            (ticket_id,),
        ).fetchall()


@retry_on_busy
def get_ticket_comment(comment_id):
    """Return a single ticket comment by id."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT c.*, COALESCE(u.username, 'deleted user') AS author_name "
            "FROM kanban_ticket_comments c "
            "LEFT JOIN users u ON c.user_id = u.id "
            "WHERE c.id = ?",
            (comment_id,),
        ).fetchone()


def update_ticket_comment(comment_id, content):
    """Update the content of a ticket comment."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE kanban_ticket_comments SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, comment_id),
        )
        conn.commit()


def delete_ticket_comment(comment_id):
    """Delete a ticket comment."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM kanban_ticket_comments WHERE id = ?", (comment_id,))
        conn.commit()


def add_activity_log(board_id, user_id, action, details=""):
    """Record an activity event for a Kanban board."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO kanban_activity_log (board_id, user_id, action, details, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (board_id, user_id, action, details, now),
        )
        conn.commit()


@retry_on_busy
def get_activity_log(board_id, limit=50):
    """Return the most recent activity entries for a board."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT al.*, u.username "
            "FROM kanban_activity_log al "
            "LEFT JOIN users u ON al.user_id = u.id "
            "WHERE al.board_id = ? "
            "ORDER BY al.created_at DESC LIMIT ?",
            (board_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# Append-only event log backing real-time collaboration.
# The kanban routes already perform fine-grained mutations (create / move /
# update / delete tickets and columns).  We layer an append-only event log
# on top so other connected clients can poll for changes and apply them
# without having to redownload the full board state.

def _kanban_next_seq(conn, board_id):
    """Return the next event sequence number for *board_id*."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM kanban_events WHERE board_id = ?",
        (board_id,),
    ).fetchone()
    return int(row["m"] or 0) + 1


def append_event(board_id, op_type, payload=None, *, by_user_id=None, by_session=""):
    """Append a single kanban operation event for *board_id*.

    *op_type* is a short string identifier (e.g. ``column_created``,
    ``ticket_moved``).  *payload* is a JSON-serialisable dict.  The function
    is best-effort: it swallows DB errors so failures here never break the
    primary mutation (the route has already committed).

    Returns the assigned sequence number, or ``None`` on failure.
    """
    if not op_type:
        return None
    by_session = (by_session or "")[:64]
    try:
        encoded = json.dumps(payload or {})
    except (TypeError, ValueError):
        encoded = "{}"
    try:
        with get_db_context() as conn:
            if not conn.execute(
                "SELECT 1 FROM kanban_boards WHERE id = ?", (board_id,),
            ).fetchone():
                return None
            seq = _kanban_next_seq(conn, board_id)
            conn.execute(
                "INSERT INTO kanban_events "
                "(board_id, seq, op_type, payload, by_user_id, by_session) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (board_id, seq, op_type, encoded, by_user_id, by_session),
            )
            conn.commit()
            return seq
    except Exception:  # noqa: BLE001 (never break the calling mutation)
        return None


@retry_on_busy
def latest_event_seq(board_id):
    """Return the latest event sequence number for *board_id* (0 if none)."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM kanban_events WHERE board_id = ?",
            (board_id,),
        ).fetchone()
        return int(row["m"] or 0)


@retry_on_busy
def events_since(board_id, since_seq, *, exclude_session=None, limit=500):
    """Return kanban events with ``seq > since_seq`` (ascending).

    Optionally filters out the calling session's own echoes.
    """
    if since_seq is None:
        since_seq = 0
    params = [board_id, int(since_seq)]
    sql = (
        "SELECT id, seq, op_type, payload, by_user_id, by_session, created_at "
        "FROM kanban_events "
        "WHERE board_id = ? AND seq > ? "
    )
    if exclude_session:
        sql += "AND by_session != ? "
        params.append(str(exclude_session)[:64])
    sql += "ORDER BY seq ASC LIMIT ?"
    params.append(int(limit))
    with get_db_context() as conn:
        return conn.execute(sql, params).fetchall()


def prune_events(board_id, keep=2000):
    """Cap the kanban event log at *keep* most-recent rows for *board_id*."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM kanban_events WHERE board_id = ?",
            (board_id,),
        ).fetchone()
        if not row or (row["n"] or 0) <= keep:
            return 0
        threshold = conn.execute(
            "SELECT seq FROM kanban_events WHERE board_id = ? "
            "ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (board_id, int(keep)),
        ).fetchone()
        if not threshold:
            return 0
        cur = conn.execute(
            "DELETE FROM kanban_events WHERE board_id = ? AND seq <= ?",
            (board_id, int(threshold["seq"])),
        )
        conn.commit()
        return cur.rowcount or 0


# Board-level history, for rollback.  Each entry stores a JSON snapshot of the entire board state (title,
# description, columns, tickets, assignees) so an editor can roll back
# a board to any previous revision, just like the wiki page-history
# feature.  Attachments and comments are intentionally NOT snapshotted:
# they continue to live in their own tables and are unaffected by
# reverts.

@retry_on_busy
def serialize_board_state(board_id):
    """Return a JSON string capturing the structural state of a board.

    The structure mirrors the kanban export format but trims attachment
    metadata (which lives in its own tables and is unaffected by
    reverts).  Tickets are listed under their owning column so the
    revert can rebuild columns/tickets in the original order.
    """
    with get_db_context() as conn:
        board_row = conn.execute(
            "SELECT id, title, description FROM kanban_boards WHERE id = ?",
            (board_id,),
        ).fetchone()
        if not board_row:
            return None
        columns = conn.execute(
            "SELECT id, title, sort_order FROM kanban_columns "
            "WHERE board_id = ? ORDER BY sort_order, id",
            (board_id,),
        ).fetchall()
        cols_data = []
        for col in columns:
            tickets = conn.execute(
                "SELECT id, title, description, priority, sort_order, "
                "due_date, color, labels FROM kanban_tickets "
                "WHERE column_id = ? ORDER BY sort_order, id",
                (col["id"],),
            ).fetchall()
            tickets_data = []
            for tk in tickets:
                assignees = conn.execute(
                    "SELECT u.username FROM kanban_ticket_assignees a "
                    "JOIN users u ON a.user_id = u.id "
                    "WHERE a.ticket_id = ? ORDER BY u.username",
                    (tk["id"],),
                ).fetchall()
                tickets_data.append({
                    "title": tk["title"] or "",
                    "description": tk["description"] or "",
                    "priority": tk["priority"] or "medium",
                    "sort_order": int(tk["sort_order"] or 0),
                    "due_date": tk["due_date"] if "due_date" in tk.keys() else None,
                    "color": (tk["color"] if "color" in tk.keys() else "") or "",
                    "labels": json.loads((tk["labels"] if "labels" in tk.keys() else "") or "[]"),
                    "assignee_usernames": [a["username"] for a in assignees],
                })
            cols_data.append({
                "title": col["title"] or "",
                "sort_order": int(col["sort_order"] or 0),
                "tickets": tickets_data,
            })
    return json.dumps({
        "title": board_row["title"] or "",
        "description": board_row["description"] or "",
        "columns": cols_data,
    }, sort_keys=True)


def _latest_history_snapshot(conn, board_id):
    """Return the most recent serialized snapshot for *board_id* or None."""
    row = conn.execute(
        "SELECT snapshot FROM kanban_board_history "
        "WHERE board_id = ? ORDER BY id DESC LIMIT 1",
        (board_id,),
    ).fetchone()
    return row["snapshot"] if row else None


def record_board_history(board_id, edited_by, edit_message, is_revert=False):
    """Capture a snapshot of *board_id*'s state.

    The function deduplicates against the most recent snapshot so a
    no-op mutation does not generate redundant history rows.  Returns
    the new history id, or ``None`` if the snapshot was a duplicate or
    the board no longer exists.
    """
    snapshot = serialize_board_state(board_id)
    if snapshot is None:
        return None
    with get_db_context() as conn:
        board_row = conn.execute(
            "SELECT title, description FROM kanban_boards WHERE id = ?",
            (board_id,),
        ).fetchone()
        if not board_row:
            return None
        prev = _latest_history_snapshot(conn, board_id)
        if prev == snapshot and not is_revert:
            return None
        cur = conn.execute(
            "INSERT INTO kanban_board_history "
            "(board_id, title, description, snapshot, edited_by, "
            " edit_message, is_revert, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                board_id,
                board_row["title"] or "",
                board_row["description"] or "",
                snapshot,
                edited_by,
                edit_message or "",
                1 if is_revert else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_board_history(board_id):
    """Return all history entries for a board, newest first."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT h.*, u.username "
            "FROM kanban_board_history h "
            "LEFT JOIN users u ON h.edited_by = u.id "
            "WHERE h.board_id = ? "
            "ORDER BY h.id DESC",
            (board_id,),
        ).fetchall()
    return rows


@retry_on_busy
def get_board_history_entry(entry_id):
    """Return a single board-history row with the editor's username."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT h.*, u.username "
            "FROM kanban_board_history h "
            "LEFT JOIN users u ON h.edited_by = u.id "
            "WHERE h.id = ?",
            (entry_id,),
        ).fetchone()
    return row


def delete_board_history_entry(entry_id):
    """Delete a single board-history row."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_board_history WHERE id = ?",
            (entry_id,),
        )
        conn.commit()


def clear_board_history(board_id):
    """Delete every history entry attached to a board."""
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM kanban_board_history WHERE board_id = ?",
            (board_id,),
        )
        conn.commit()


def restore_board_from_snapshot(board_id, snapshot_json):
    """Rebuild *board_id*'s columns and tickets from *snapshot_json*.

    Existing columns and tickets are removed first; attachments and
    comments survive only when their owning ticket title matches one
    in the snapshot: for simplicity we delete everything and rebuild
    from the snapshot, which mirrors the wiki "revert" semantics where
    inline content is fully replaced.

    Returns a list of attachment filenames that were detached from
    rebuilt tickets so the caller can remove them from disk if the
    snapshot no longer references them.
    """
    try:
        state = json.loads(snapshot_json or "{}")
    except (TypeError, ValueError):
        state = {}
    columns = state.get("columns") if isinstance(state, dict) else None
    if not isinstance(columns, list):
        columns = []
    title = (state.get("title") or "").strip() if isinstance(state, dict) else ""
    description = (state.get("description") or "") if isinstance(state, dict) else ""

    orphaned_files = []
    with get_db_context() as conn:
        board_row = conn.execute(
            "SELECT id FROM kanban_boards WHERE id = ?",
            (board_id,),
        ).fetchone()
        if not board_row:
            return orphaned_files

        # Collect attachment files for tickets that will be removed.
        attach_rows = conn.execute(
            "SELECT ka.filename FROM kanban_ticket_attachments ka "
            "JOIN kanban_tickets kt ON ka.ticket_id = kt.id "
            "JOIN kanban_columns kc ON kt.column_id = kc.id "
            "WHERE kc.board_id = ?",
            (board_id,),
        ).fetchall()
        orphaned_files = [r["filename"] for r in attach_rows]

        # Wipe existing columns (cascade removes tickets, assignees,
        # attachments, comments).
        conn.execute(
            "DELETE FROM kanban_columns WHERE board_id = ?",
            (board_id,),
        )

        # Update board title/description from the snapshot.  Empty
        # title is ignored so reverts never leave a board untitled.
        set_parts = []
        vals = []
        if title:
            set_parts.append("title=?")
            vals.append(title)
        set_parts.append("description=?")
        vals.append(description)
        if set_parts:
            vals.append(board_id)
            conn.execute(
                f"UPDATE kanban_boards SET {', '.join(set_parts)} WHERE id=?",
                vals,
            )

        # Resolve assignee usernames -> user ids in one query.
        all_names = set()
        for col in columns:
            for tk in (col.get("tickets") or []):
                for name in (tk.get("assignee_usernames") or []):
                    if isinstance(name, str) and name.strip():
                        all_names.add(name.strip())
        username_to_id = {}
        if all_names:
            placeholders = ",".join(["?"] * len(all_names))
            user_rows = conn.execute(
                f"SELECT id, username FROM users WHERE username IN ({placeholders})",
                tuple(all_names),
            ).fetchall()
            username_to_id = {r["username"]: r["id"] for r in user_rows}

        for col_idx, col in enumerate(columns):
            col_title = (col.get("title") or "").strip() or "Column"
            col_sort = int(col.get("sort_order") or col_idx)
            cur_col = conn.execute(
                "INSERT INTO kanban_columns (board_id, title, sort_order) "
                "VALUES (?, ?, ?)",
                (board_id, col_title, col_sort),
            )
            col_id = cur_col.lastrowid
            for tk_idx, tk in enumerate(col.get("tickets") or []):
                tk_title = (tk.get("title") or "").strip() or "Ticket"
                tk_desc = tk.get("description") or ""
                tk_priority = tk.get("priority") or "medium"
                if tk_priority not in ("low", "medium", "high", "critical"):
                    tk_priority = "medium"
                tk_sort = int(tk.get("sort_order") or tk_idx)
                tk_due = tk.get("due_date") or None
                tk_color = tk.get("color") or ""
                tk_labels = json.dumps(tk.get("labels") or [])
                cur_tk = conn.execute(
                    "INSERT INTO kanban_tickets "
                    "(column_id, title, description, priority, sort_order, "
                    " due_date, color, labels) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (col_id, tk_title, tk_desc, tk_priority, tk_sort,
                     tk_due, tk_color, tk_labels),
                )
                ticket_id = cur_tk.lastrowid
                for name in (tk.get("assignee_usernames") or []):
                    if not isinstance(name, str):
                        continue
                    uid = username_to_id.get(name.strip())
                    if uid:
                        conn.execute(
                            "INSERT OR IGNORE INTO kanban_ticket_assignees "
                            "(ticket_id, user_id) VALUES (?, ?)",
                            (ticket_id, uid),
                        )
        conn.commit()
    return orphaned_files
