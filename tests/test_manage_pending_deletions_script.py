"""Tests for scripts/manage_pending_deletions.py."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import db


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "manage_pending_deletions.py"
_SPEC = spec_from_file_location("manage_pending_deletions_script", _SCRIPT_PATH)
manage_pending_deletions = module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(manage_pending_deletions)


def test_list_reports_pending_pages(admin_user):
    page_id = db.create_page("Script Pending", "script-pending", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    lines = []
    rc = manage_pending_deletions.execute("list", out=lines.append)
    assert rc == 0
    assert any("Deletion Slowdown plugin: enabled" in line for line in lines)
    assert any("Pending pages: 1" in line for line in lines)
    assert any("script-pending" in line for line in lines)


def test_restore_all_restores_every_pending_page(admin_user):
    p1 = db.create_page("Restore A", "restore-a", "content")
    p2 = db.create_page("Restore B", "restore-b", "content")
    db.mark_page_pending_deletion(p1, admin_user)
    db.mark_page_pending_deletion(p2, admin_user)
    lines = []
    rc = manage_pending_deletions.execute("restore-all", yes=True, out=lines.append)
    assert rc == 0
    assert db.get_page(p1)["pending_deletion"] == 0
    assert db.get_page(p2)["pending_deletion"] == 0
    assert any("Restored 2 page(s)." in line for line in lines)


def test_purge_all_permanently_deletes_pending_pages(admin_user):
    page_id = db.create_page("Purge Me", "purge-me", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    lines = []
    rc = manage_pending_deletions.execute("purge-all", yes=True, out=lines.append)
    assert rc == 0
    assert db.get_page(page_id) is None
    assert any("Permanently purged 1 page(s)." in line for line in lines)


def test_restore_all_blocked_when_plugin_disabled_without_override(admin_user):
    page_id = db.create_page("Blocked Restore", "blocked-restore", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    db.disable_plugin("deletion_slowdown")
    lines = []
    rc = manage_pending_deletions.execute("restore-all", yes=True, out=lines.append)
    assert rc == 2
    assert db.get_page(page_id)["pending_deletion"] == 1
    assert any("Refusing to run bulk restore/purge" in line for line in lines)


def test_restore_all_allowed_when_plugin_disabled_with_override(admin_user):
    page_id = db.create_page("Override Restore", "override-restore", "content")
    db.mark_page_pending_deletion(page_id, admin_user)
    db.disable_plugin("deletion_slowdown")
    lines = []
    rc = manage_pending_deletions.execute(
        "restore-all",
        yes=True,
        allow_disabled_plugin=True,
        out=lines.append,
    )
    assert rc == 0
    assert db.get_page(page_id)["pending_deletion"] == 0
    assert any("Deletion Slowdown plugin: disabled" in line for line in lines)
