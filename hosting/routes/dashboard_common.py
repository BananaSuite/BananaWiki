"""Shared hosting access checks, account notices, and streamed downloads."""

import logging
import os
import shutil
import time
from flask import (
    Response,
    request,
    stream_with_context,
    url_for,
)
from .. import config
from ..db import (
    collaborator_has_permission,
)
from ..email_delivery import (
    is_configured as email_is_configured,
    render_email_html,
    send_email,
)

logger = logging.getLogger(__name__)


_EXPORT_DOWNLOAD_MAX_AGE_SECONDS = 24 * 60 * 60

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _send_account_notice(account, subject, text):
    """Best-effort operational notice to a saved contact address."""
    email = (account or {}).get("email") or ""
    if not email or not email_is_configured():
        return False
    ok, error = send_email(
        to=email,
        subject=subject,
        text=text,
        html=render_email_html(
            title=subject.replace(": BananaWiki", ""),
            eyebrow="ACCOUNT NOTICE",
            text=text,
        ),
    )
    if not ok:
        logger.warning(
            "Operational email to account %s failed: %s",
            account.get("id"),
            error,
        )
    return ok


def _cleanup_download_artifacts(paths=(), dirs=()):
    for path in paths:
        if not path:
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to delete temporary download file %s", path)
    for path in dirs:
        if not path:
            continue
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to delete temporary download dir %s", path)


def _stream_download_file(
    path,
    download_name,
    mimetype,
    *,
    cleanup_paths=(),
    cleanup_dirs=(),
):
    """Stream a temporary download and clean it up after iteration finishes."""
    cleanup_paths = tuple(cleanup_paths or ()) or (path,)
    cleanup_dirs = tuple(cleanup_dirs or ())
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    @stream_with_context
    def _iter_file():
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            _cleanup_download_artifacts(cleanup_paths, cleanup_dirs)

    response = Response(_iter_file(), mimetype=mimetype)
    response.headers.set("Content-Disposition", "attachment", filename=download_name)
    response.headers["Content-Length"] = str(size)
    response.headers["X-Accel-Buffering"] = "no"
    return response


def _export_download_root():
    root = config.HOSTING_EXPORT_TEMP_DIR
    os.makedirs(root, exist_ok=True)
    return root


def _cleanup_stale_export_downloads():
    root = _export_download_root()
    cutoff = time.time() - _EXPORT_DOWNLOAD_MAX_AGE_SECONDS
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith("bwh-archive-"):
            continue
        path = os.path.join(root, entry)
        try:
            if os.path.getmtime(path) < cutoff:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
        except OSError:
            pass


def _owner_is_locked_out(inst, account_id, is_admin):
    """Return ``True`` if a non-admin owner is currently locked out of
    interacting with their suspended instance.

    Admins are NEVER locked out by suspension. They still need to be
    able to manage / unsuspend / terminate from the admin tools.  When
    locked out the caller should flash a clear suspension notice and
    redirect; do NOT silently no-op because that hides the reason.
    """
    if inst is None:
        return False
    if is_admin:
        return False
    if inst["account_id"] != account_id:
        return False
    return inst["status"] == "suspended"


_SUSPENDED_LOCKOUT_MESSAGE = (
    "This wiki has been suspended by an administrator and cannot be "
    "modified. Contact support to have it unsuspended."
)


def _can_access_instance(inst, account_id, is_admin, permission="view"):
    """Return ``True`` if *account_id* may access *inst* with *permission*.

    Access is granted when the caller is:

    * the instance **owner** (all permissions implicitly),
    * a **platform admin** (all permissions implicitly), or
    * a **collaborator** who holds the requested *permission*
      (``full_access`` collaborators hold every permission).
    """
    if inst is None:
        return False
    if is_admin:
        return True
    if inst["account_id"] == account_id:
        return True
    return collaborator_has_permission(inst["id"], account_id, permission)


def _is_owner_or_admin(inst, account_id, is_admin):
    """Return ``True`` if *account_id* is the owner or a platform admin.

    Used for actions that only owners and platform admins should perform
    (e.g. managing collaborators, initiating transfers).  Collaborators,
    even ``full_access`` ones: are excluded.
    """
    if inst is None:
        return False
    if is_admin:
        return True
    return inst["account_id"] == account_id


def _post_instance_admin_action_redirect(instance_id):
    """Pick the right URL to redirect to after an admin action on an instance.

    Admin actions can now be triggered from three different places:

    * The dedicated admin management page
      (``/admin/instances/<id>/manage``): go back there.
    * The instance detail page (``/instances/<id>``): go back
      there so admins can keep working from the same view they
      clicked from without bouncing through the admin dashboard.
    * Anywhere else (admin dashboard, direct POST, etc.): fall
      back to the admin dashboard.

    Detection is based on the ``Referer`` header.
    """
    ref = request.referrer or ""
    if f"/admin/instances/{instance_id}/manage" in ref:
        return url_for("hosting_admin_instance_manage", instance_id=instance_id)
    if ref.rstrip("/").endswith(f"/instances/{instance_id}"):
        return url_for("hosting_instance_detail", instance_id=instance_id)
    return url_for("hosting_admin")
