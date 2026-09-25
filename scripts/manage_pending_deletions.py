#!/usr/bin/env python3
"""
BananaWiki Pending Deletions Maintenance Tool

SSH-only administrative script for bulk operations on pages queued by the
deletion_slowdown plugin.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def _print_pending_rows(pending, out):
    """Print pending-deletion pages in a compact tabular format."""
    if not pending:
        out("No pages are currently pending deletion.")
        return
    out(f"{'#':<4} {'ID':<6} {'Slug':<36} {'Title'}")
    out("-" * 90)
    for idx, row in enumerate(pending, start=1):
        out(f"{idx:<4} {row['id']:<6} {row['slug']:<36} {row['title']}")


def _confirm(prompt, input_fn):
    """Return True when the operator confirms with 'y' or 'yes'."""
    raw = input_fn(f"{prompt} [y/N]: ").strip().lower()
    return raw in ("y", "yes")


def _restore_all(pending):
    """Restore all pending pages and return (restored_count, failed_ids)."""
    restored = 0
    failed_ids = []
    for row in pending:
        try:
            if db.restore_page_from_pending_deletion(row["id"]):
                restored += 1
            else:
                failed_ids.append(row["id"])
        except Exception:
            failed_ids.append(row["id"])
    return restored, failed_ids


def _purge_all(pending):
    """Permanently delete all pending pages and return (purged_count, failed_ids)."""
    purged = 0
    failed_ids = []
    for row in pending:
        try:
            if db.get_page(row["id"]):
                db.delete_page(row["id"])
                purged += 1
            else:
                failed_ids.append(row["id"])
        except Exception:
            failed_ids.append(row["id"])
    return purged, failed_ids


def execute(action, *, yes=False, allow_disabled_plugin=False, input_fn=input, out=print):
    """Execute one maintenance action and return a process exit code."""
    db.init_db()
    enabled = db.is_plugin_enabled("deletion_slowdown")
    pending = db.list_pending_deletions()

    out(f"Deletion Slowdown plugin: {'enabled' if enabled else 'disabled'}")
    out(f"Pending pages: {len(pending)}")

    if action == "list":
        _print_pending_rows(pending, out)
        if not enabled and pending:
            out(
                "WARNING: Plugin is disabled while pending pages exist. "
                "Pages stay pending until restored or purged, and web restore routes stay unavailable."
            )
        return 0

    if not enabled and not allow_disabled_plugin:
        out(
            "Refusing to run bulk restore/purge while deletion_slowdown is disabled. "
            "Re-run with --allow-disabled-plugin to proceed over SSH."
        )
        return 2

    if not pending:
        out("No pending pages found.")
        return 0

    if action == "restore-all":
        if not yes and not _confirm("Restore all pending pages?", input_fn):
            out("Aborted.")
            return 1
        restored, failed_ids = _restore_all(pending)
        out(f"Restored {restored} page(s).")
        if failed_ids:
            out(f"Failed to restore {len(failed_ids)} page(s): {failed_ids}")
        return 0

    if action == "purge-all":
        if not yes and not _confirm(
            "Force purge ALL pending pages (permanent delete)?", input_fn
        ):
            out("Aborted.")
            return 1
        purged, failed_ids = _purge_all(pending)
        out(f"Permanently purged {purged} page(s).")
        if failed_ids:
            out(f"Failed to purge {len(failed_ids)} page(s): {failed_ids}")
        return 0

    out("Unknown action.")
    return 2


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Bulk-manage pages in pending-deletion state (SSH-only).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="List pending pages (default action).")
    group.add_argument("--restore-all", action="store_true", help="Restore all pending pages.")
    group.add_argument(
        "--purge-all",
        action="store_true",
        help="Permanently delete all pending pages.",
    )
    parser.add_argument(
        "--allow-disabled-plugin",
        action="store_true",
        help=(
            "Allow restore/purge even when deletion_slowdown is disabled. "
            "Useful for emergency cleanup after plugin disable."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompts.",
    )
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.restore_all:
        action = "restore-all"
    elif args.purge_all:
        action = "purge-all"
    else:
        action = "list"
    return execute(
        action,
        yes=args.yes,
        allow_disabled_plugin=args.allow_disabled_plugin,
    )


if __name__ == "__main__":
    raise SystemExit(main())
