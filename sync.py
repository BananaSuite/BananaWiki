"""
BananaWiki: Sync Stub Module

This module previously implemented a Telegram-based backup/sync system
(~2 300 lines) that automatically shipped runtime artifacts to a Telegram
chat whenever significant changes occurred.  That functionality has been
fully removed.

All public symbols that other modules import are retained here as no-ops
so the rest of the codebase continues to work without modification.
"""
from __future__ import annotations

# Set high on purpose: with sync disabled this constant no longer mirrors
# Telegram's 50 MB upload ceiling and must not restrict uploads on its own.
TELEGRAM_FILE_LIMIT: int = 50_000_000


def notify_change(change_type: str, description: str = "") -> None:
    """No-op.  Previously queued a backup after a wiki change."""


def notify_file_upload(filename: str, filepath: str, display_name: str = "") -> None:
    """No-op.  Previously notified the sync system of a new file upload."""


def notify_file_deleted(filename: str) -> None:
    """No-op.  Previously notified the sync system of a file deletion."""


def notify_cleanup_completed(summary: dict) -> None:
    """No-op.  Previously sent a cleanup-summary message to Telegram."""


def backup_chats_before_cleanup() -> None:
    """No-op.  Chat cleanup no longer creates Telegram backups."""


def backup_group_chats_before_cleanup() -> None:
    """No-op.  Group-chat cleanup no longer creates Telegram backups."""


def cleanup_stale_upload_msg_store() -> int:
    """No-op.  The removed Telegram upload-message store has no stale rows."""
    return 0


def start_daily_backup_scheduler() -> None:
    """No-op.  Previously started a background thread for daily backups."""


def is_enabled() -> bool:
    """Sync is permanently disabled."""
    return False
