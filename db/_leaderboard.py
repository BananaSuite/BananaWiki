"""Contributor leaderboard stats computed from page_history."""

import csv
import io
from collections import defaultdict

from helpers._diff import compute_char_diff

from ._connection import get_db_context


LEADERBOARD_SORTS = ("score", "edits", "chars", "pages", "active", "recent")

LEADERBOARD_DATE_RANGES = {
    "all": None,
    "30d": 30,
    "90d": 90,
    "year": 365,
}


def _blank_stats(user_id):
    """Return the mutable accumulator used while walking revision history."""
    return {
        "user_id": user_id,
        "edit_count": 0,
        "pages_touched": set(),
        "pages_created": 0,
        "active_days": set(),
        "added": 0,
        "deleted": 0,
        "revert_count": 0,
        "metadata_edits": 0,
        "first_contribution": None,
        "first_revision_id": None,
        "last_contribution": None,
        "last_revision_id": None,
        "last_page_id": None,
        "last_page_title": "",
        "last_page_slug": "",
        "last_edit_message": "",
        "largest_edit": 0,
        "largest_edit_at": None,
        "largest_edit_title": "",
        "longest_streak": 0,
        "current_streak": 0,
    }


def _normal_user_id(value):
    """Return a user id string for human contributors, otherwise ``None``."""
    if value is None:
        return None
    user_id = str(value)
    if not user_id or user_id == "-1":
        return None
    return user_id


def _date_key(value):
    """Return the YYYY-MM-DD part of an ISO-ish timestamp."""
    if not value:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _is_newer(candidate, candidate_id, current, current_id):
    """Return True when *candidate* is the later history entry, breaking ties on
    the larger row id.  A missing *current* always loses."""
    if not current:
        return True
    if not candidate:
        return False
    if candidate == current:
        return int(candidate_id or 0) > int(current_id or 0)
    return candidate > current


def _is_older(candidate, candidate_id, current, current_id):
    """Return True when *candidate* is the earlier history entry, breaking ties on
    the smaller row id.  A missing *current* always loses."""
    if not current:
        return True
    if not candidate:
        return False
    if candidate == current:
        return int(candidate_id or 0) < int(current_id or 0)
    return candidate < current


def _compute_streak(active_days):
    """Return (longest_streak, current_streak) from a set of YYYY-MM-DD strings."""
    if not active_days:
        return 0, 0
    sorted_days = sorted(active_days)
    longest = 1
    current = 1
    for i in range(1, len(sorted_days)):
        prev = sorted_days[i - 1]
        curr = sorted_days[i]
        if len(prev) >= 10 and len(curr) >= 10:
            from datetime import datetime, timedelta
            try:
                prev_dt = datetime.strptime(prev[:10], "%Y-%m-%d")
                curr_dt = datetime.strptime(curr[:10], "%Y-%m-%d")
                if curr_dt - prev_dt == timedelta(days=1):
                    current += 1
                    longest = max(longest, current)
                else:
                    current = 1
            except ValueError:
                current = 1
        else:
            current = 1
    return longest, current


def export_leaderboard_csv(entries):
    """Return a CSV string for the given leaderboard entries."""
    output = io.StringIO()
    fieldnames = [
        "rank", "username", "score", "edit_count", "pages_touched",
        "pages_created", "active_days", "longest_streak", "current_streak",
        "added", "deleted", "net", "avg_change", "largest_edit",
        "revert_count", "metadata_edits", "first_contribution", "last_contribution",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for entry in entries:
        row = {k: entry.get(k, "") for k in fieldnames}
        writer.writerow(row)
    return output.getvalue()


def _impact_score(entry):
    """Calculate a compact, multi-signal contribution score.

    Weights are tuned to reward breadth of contribution (pages touched,
    creation of new pages), consistency (active days, streak), volume
    of meaningful edits, and penalise reverts.  The formula was chosen
    so that a single-page editor with ~50 edits sits around 600 points
    while a prolific cross-page contributor can reach 3 000+.
    """
    total_changed = entry["added"] + entry["deleted"]
    net_positive = max(entry["added"] - entry["deleted"], 0)
    streak_bonus = entry.get("longest_streak", 0) * 4
    score = (
        entry["edit_count"] * 10
        + entry["pages_touched"] * 20
        + entry["pages_created"] * 30
        + entry["active_days"] * 8
        + streak_bonus
        + total_changed / 120
        + net_positive / 250
        - entry["revert_count"] * 3
    )
    return max(0, int(round(score)))


def _sort_entries(entries, sort_by):
    """Sort entries in place and attach rank/primary values."""
    if sort_by not in LEADERBOARD_SORTS:
        sort_by = "score"

    def primary(entry):
        """Return the primary sort value for the given leaderboard entry."""
        if sort_by == "edits":
            return entry["edit_count"]
        if sort_by == "chars":
            return entry["total"]
        if sort_by == "pages":
            return entry["pages_touched"]
        if sort_by == "active":
            return entry["active_days"]
        if sort_by == "recent":
            return entry["last_contribution"] or ""
        return entry["score"]

    entries.sort(key=lambda entry: entry["username"].casefold())
    entries.sort(
        key=lambda entry: (
            primary(entry),
            entry["score"],
            entry["edit_count"],
            entry["total"],
            entry["pages_touched"],
        ),
        reverse=True,
    )
    for index, entry in enumerate(entries, start=1):
        entry["rank"] = index
        entry["primary_value"] = primary(entry)
    return entries


def get_leaderboard_stats(limit=100, sort_by="score", include_user_id=None, since=None):
    """Return ranked contributor stats, podium entries, and global totals.

    The stats are derived from consecutive page_history revisions per page.
    Initial page content counts as added text for the creating contributor.

    Args:
        limit: Maximum number of entries to return (0 for all).
        sort_by: One of LEADERBOARD_SORTS.
        include_user_id: If set, ensure this user appears even outside *limit*.
        since: Optional date string (YYYY-MM-DD).  Only revisions on or after
               this date are counted.  ``None`` means no lower bound.
    """
    if sort_by not in LEADERBOARD_SORTS:
        sort_by = "score"
    include_user_id = _normal_user_id(include_user_id)

    with get_db_context() as conn:
        if since:
            rows = conn.execute(
                "SELECT ph.id, ph.page_id, ph.title, ph.content, ph.edited_by, "
                "ph.edit_message, ph.is_revert, ph.created_at, p.slug AS page_slug "
                "FROM page_history ph "
                "LEFT JOIN pages p ON p.id = ph.page_id "
                "WHERE ph.created_at >= ? "
                "ORDER BY ph.page_id ASC, ph.created_at ASC, ph.id ASC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ph.id, ph.page_id, ph.title, ph.content, ph.edited_by, "
                "ph.edit_message, ph.is_revert, ph.created_at, p.slug AS page_slug "
                "FROM page_history ph "
                "LEFT JOIN pages p ON p.id = ph.page_id "
                "ORDER BY ph.page_id ASC, ph.created_at ASC, ph.id ASC"
            ).fetchall()

    revisions_by_page = defaultdict(list)
    for row in rows:
        revisions_by_page[row["page_id"]].append(row)

    user_stats = {}
    summary_pages = set()
    summary_days = set()

    for page_id, revisions in revisions_by_page.items():
        previous_content = ""
        for index, revision in enumerate(revisions):
            user_id = _normal_user_id(revision["edited_by"])
            content = revision["content"] or ""
            added, deleted = compute_char_diff(previous_content, content)
            previous_content = content

            if not user_id:
                continue

            stats = user_stats.setdefault(user_id, _blank_stats(user_id))
            changed = added + deleted
            created_at = revision["created_at"]
            day = _date_key(created_at)

            stats["edit_count"] += 1
            stats["pages_touched"].add(page_id)
            stats["added"] += added
            stats["deleted"] += deleted
            if index == 0:
                stats["pages_created"] += 1
            if day:
                stats["active_days"].add(day)
                summary_days.add(day)
            if revision["is_revert"]:
                stats["revert_count"] += 1
            if changed == 0:
                stats["metadata_edits"] += 1
            if _is_older(
                created_at,
                revision["id"],
                stats["first_contribution"],
                stats["first_revision_id"],
            ):
                stats["first_contribution"] = created_at
                stats["first_revision_id"] = revision["id"]
            if _is_newer(
                created_at,
                revision["id"],
                stats["last_contribution"],
                stats["last_revision_id"],
            ):
                stats["last_contribution"] = created_at
                stats["last_revision_id"] = revision["id"]
                stats["last_page_id"] = page_id
                stats["last_page_title"] = revision["title"] or ""
                stats["last_page_slug"] = revision["page_slug"] or ""
                stats["last_edit_message"] = revision["edit_message"] or ""
            if changed > stats["largest_edit"]:
                stats["largest_edit"] = changed
                stats["largest_edit_at"] = created_at
                stats["largest_edit_title"] = revision["title"] or ""

            summary_pages.add(page_id)

    if not user_stats:
        return {
            "entries": [],
            "podium": [],
            "summary": {
                "contributor_count": 0,
                "edit_count": 0,
                "pages_touched": 0,
                "pages_created": 0,
                "active_days": 0,
                "added": 0,
                "deleted": 0,
                "total": 0,
                "net": 0,
                "revert_count": 0,
                "last_contribution": None,
            },
            "sort_by": sort_by,
        }

    user_ids = list(user_stats.keys())
    placeholders = ",".join("?" * len(user_ids))
    with get_db_context() as conn:
        user_rows = conn.execute(
            f"SELECT id, username FROM users WHERE id IN ({placeholders})",
            user_ids,
        ).fetchall()
    username_map = {str(row["id"]): row["username"] for row in user_rows}

    entries = []
    for user_id, stats in user_stats.items():
        username = username_map.get(user_id)
        if not username:
            continue
        pages_touched = len(stats["pages_touched"])
        active_days = len(stats["active_days"])
        total_changed = stats["added"] + stats["deleted"]
        longest_streak, current_streak = _compute_streak(stats["active_days"])
        entry = {
            "username": username,
            "user_id": user_id,
            "edit_count": stats["edit_count"],
            "pages_touched": pages_touched,
            "pages_created": stats["pages_created"],
            "active_days": active_days,
            "added": stats["added"],
            "deleted": stats["deleted"],
            "total": total_changed,
            "net": stats["added"] - stats["deleted"],
            "avg_change": int(round(total_changed / stats["edit_count"])) if stats["edit_count"] else 0,
            "revert_count": stats["revert_count"],
            "metadata_edits": stats["metadata_edits"],
            "first_contribution": stats["first_contribution"],
            "last_contribution": stats["last_contribution"],
            "last_page_id": stats["last_page_id"],
            "last_page_title": stats["last_page_title"],
            "last_page_slug": stats["last_page_slug"],
            "last_edit_message": stats["last_edit_message"],
            "largest_edit": stats["largest_edit"],
            "largest_edit_at": stats["largest_edit_at"],
            "largest_edit_title": stats["largest_edit_title"],
            "longest_streak": longest_streak,
            "current_streak": current_streak,
        }
        entry["score"] = _impact_score(entry)
        entries.append(entry)

    _sort_entries(entries, sort_by)

    visible_entries = entries[:limit] if limit else list(entries)
    if include_user_id and all(entry["user_id"] != include_user_id for entry in visible_entries):
        included = next((entry for entry in entries if entry["user_id"] == include_user_id), None)
        if included:
            included = dict(included)
            included["outside_limit"] = True
            visible_entries.append(included)

    summary = {
        "contributor_count": len(entries),
        "edit_count": sum(entry["edit_count"] for entry in entries),
        "pages_touched": len(summary_pages),
        "pages_created": sum(entry["pages_created"] for entry in entries),
        "active_days": len(summary_days),
        "added": sum(entry["added"] for entry in entries),
        "deleted": sum(entry["deleted"] for entry in entries),
        "total": sum(entry["total"] for entry in entries),
        "net": sum(entry["net"] for entry in entries),
        "revert_count": sum(entry["revert_count"] for entry in entries),
        "last_contribution": max(
            (entry["last_contribution"] for entry in entries if entry["last_contribution"]),
            default=None,
        ),
    }

    return {
        "entries": visible_entries,
        "podium": entries[:3],
        "summary": summary,
        "sort_by": sort_by,
    }


def get_leaderboard_by_edits(limit=50, since=None):
    """Return a list of entries ordered by edit count descending."""
    return get_leaderboard_stats(limit=limit, sort_by="edits", since=since)["entries"]


def get_leaderboard_by_chars(limit=50, since=None):
    """Return a list of entries ordered by total chars changed descending."""
    return get_leaderboard_stats(limit=limit, sort_by="chars", since=since)["entries"]
