"""Admin-action email notifications for the hosting platform.

Each function sends an email to the affected user when the admin opts in
via the ``notify_user`` form checkbox.  All functions are no-ops when
email delivery is not configured or the user has no email on file.

Usage in route handlers::

    from ..notifications import notify_account_suspended
    if request.form.get("notify_user"):
        notify_account_suspended(account, reason=reason, duration=duration_text)
"""

import logging

from . import config
from .email_delivery import send_email, is_configured

_logger = logging.getLogger("hosting.notifications")


def _safe_send(*, to, subject, text):
    """Best-effort send: never raises, logs on failure.

    Returns True when the message was accepted for delivery.
    """
    if not to or not is_configured():
        return False
    try:
        ok, error = send_email(to=to, subject=subject, text=text)
        if not ok:
            _logger.warning("Notification email failed to %s: %s", to, error)
        return bool(ok)
    except Exception as exc:
        _logger.warning("Notification email error to %s: %s", to, exc)
        return False


def notify_account_suspended(account, *, reason="", duration=""):
    """Notify a user that their account has been suspended."""
    email = account.get("email")
    username = account.get("username", "there")
    body = "Hello {},\n\nYour BananaWiki Hosting account has been suspended.".format(username)
    if reason:
        body += "\n\nReason: {}".format(reason)
    if duration:
        body += "\nDuration: {}".format(duration)
    body += f"\n\nIf you believe this is a mistake, reply to this email or contact {config.HOSTING_CONTACT_EMAIL}."
    _safe_send(to=email, subject="Account Suspended: BananaWiki", text=body)


def notify_account_unsuspended(account):
    """Notify a user that their account suspension has been lifted."""
    email = account.get("email")
    username = account.get("username", "there")
    _safe_send(
        to=email,
        subject="Account Restored: BananaWiki",
        text=(f"Hello {username},\n\nYour BananaWiki Hosting account has been restored. "
              f"You can log in at {_portal_url()}.\n\n"
              f"If you have any questions, contact {config.HOSTING_CONTACT_EMAIL}."),
    )


def notify_account_pending_deletion(account, *, reason="", hours=24):
    """Notify a user that their account is scheduled for deletion."""
    email = account.get("email")
    username = account.get("username", "there")
    body = "Hello {},\n\nYour BananaWiki Hosting account has been scheduled for deletion in {} hours.".format(
        username, hours
    )
    if reason:
        body += "\n\nReason: {}".format(reason)
    body += f"\n\nIf you believe this is a mistake, contact {config.HOSTING_CONTACT_EMAIL} immediately."
    _safe_send(to=email, subject="Account Scheduled for Deletion: BananaWiki", text=body)


def notify_account_deleted(email, username):
    """Send a final confirmation that the account has been deleted."""
    body = (
        f"Hello {username},\n\nYour BananaWiki Hosting account and all associated data "
        "have been permanently deleted.\n\n"
        f"If you did not request this, contact {config.HOSTING_CONTACT_EMAIL} immediately."
    )
    _safe_send(to=email, subject="Account Deleted: BananaWiki", text=body)


def notify_account_approved(account):
    """Notify a user that their account signup has been approved."""
    email = account.get("email")
    username = account.get("username", "there")
    _safe_send(
        to=email,
        subject="Account Approved: BananaWiki",
        text=(f"Hello {username},\n\nYour BananaWiki Hosting account has been approved. "
              f"You can now log in and create wikis at {_portal_url()}."),
    )


def notify_account_denied(account):
    """Notify a user that their account signup has been denied."""
    email = account.get("email")
    username = account.get("username", "there")
    _safe_send(
        to=email,
        subject="Account Request Denied: BananaWiki",
        text=(f"Hello {username},\n\nYour BananaWiki Hosting account request has been denied.\n\n"
              f"If you have questions, contact {config.HOSTING_CONTACT_EMAIL}."),
    )


def notify_instance_created(account, subdomain):
    """Notify a user that their wiki instance has been created."""
    email = account.get("email")
    username = account.get("username", "there")
    _safe_send(
        to=email,
        subject="Wiki Created: BananaWiki",
        text=(f"Hello {username},\n\nYour wiki instance '{subdomain}' has been created and is ready to use.\n\n"
              f"Log in to your dashboard at {_portal_url('/dashboard')} to get started."),
    )


def notify_instance_terminated(account, subdomain, *, reason=""):
    """Notify a user that their wiki instance has been terminated."""
    email = account.get("email")
    username = account.get("username", "there")
    body = "Hello {},\n\nYour wiki instance '{}' has been terminated.".format(username, subdomain)
    if reason:
        body += "\n\nReason: {}".format(reason)
    body += (
        "\n\nYour data will be retained during the grace period. "
        "After that, it will be permanently deleted.\n\n"
        f"If you need to recover your data, contact {config.HOSTING_CONTACT_EMAIL}."
    )
    _safe_send(to=email, subject="Wiki Terminated: BananaWiki", text=body)


def notify_instance_suspended(account, subdomain, *, reason="", duration=""):
    """Notify a user that their wiki instance has been suspended."""
    email = account.get("email")
    username = account.get("username", "there")
    body = "Hello {},\n\nYour wiki instance '{}' has been suspended.".format(username, subdomain)
    if reason:
        body += "\n\nReason: {}".format(reason)
    if duration:
        body += "\nDuration: {}".format(duration)
    body += f"\n\nIf you believe this is a mistake, contact {config.HOSTING_CONTACT_EMAIL}."
    _safe_send(to=email, subject="Wiki Suspended: BananaWiki", text=body)


def _portal_url(path=""):
    """Absolute URL of this portal for email links, with an optional *path*.

    Works outside a request context (maintenance sweeps); falls back to
    the bare path when no public origin is configured.
    """
    try:
        if config.HOSTING_MODE == "subdomain":
            host = config.EFFECTIVE_PORTAL_DOMAIN or config.BASE_DOMAIN
            scheme = "https"
        else:
            from .instance_paths import _format_host_for_url
            host = config.HOSTING_PUBLIC_HOST
            scheme = config.HOSTING_PUBLIC_SCHEME
            if host:
                host = _format_host_for_url(host)
                if config.HOSTING_PORT not in (80, 443):
                    host = f"{host}:{config.HOSTING_PORT}"
        if not host:
            return path or "/"
        return f"{scheme}://{host}{path}"
    except Exception:
        return path or "/"


def _admin_dashboard_url():
    """Absolute URL of the hosting admin dashboard for email links."""
    return _portal_url("/admin")


def notify_admin_signup_pending(admin_email, account):
    """Notify the admin address that one signup awaits approval.

    Immediate mode: one email per pending signup.  Best-effort. Never
    raises, so signup can never fail because of a notification.
    Returns True when the message was accepted for delivery.
    """
    account = account or {}
    username = account.get("username", "there")
    contact = account.get("email") or "no contact email on file"
    body = (
        "A new hosting account '{}' ({}) is waiting for approval.\n\n"
        "Review it in the admin dashboard: {}".format(
            username, contact, _admin_dashboard_url()
        )
    )
    use_case = (account.get("signup_use_case") or "").strip()
    if use_case:
        body += "\n\nStated use case:\n{}".format(use_case[:2000])
    return _safe_send(
        to=admin_email,
        subject="Approval needed: new signup '{}': BananaWiki".format(username),
        text=body,
    )


def notify_admin_signup_digest(admin_email, pending):
    """Notify the admin address with the current pending-approval queue.

    Digest mode: a single email listing everyone still waiting, no matter
    how many signups arrived: this is what keeps provider quotas intact.
    Returns True when the message was accepted for delivery.
    """
    pending = list(pending or [])
    if not pending:
        return False
    lines = ["{} signup(s) waiting for approval:".format(len(pending)), ""]
    for account in pending[:50]:
        account = account or {}
        lines.append(
            "- {} ({}): signed up {}".format(
                account.get("username", "there"),
                account.get("email") or "no contact email on file",
                account.get("created_at") or "unknown time",
            )
        )
    if len(pending) > 50:
        lines.append("…and {} more.".format(len(pending) - 50))
    lines += [
        "",
        "Review them in the admin dashboard: {}".format(_admin_dashboard_url()),
    ]
    return _safe_send(
        to=admin_email,
        subject="{} signup(s) waiting for approval: BananaWiki".format(len(pending)),
        text="\n".join(lines),
    )


def maybe_send_pending_approval_digest(*, now=None):
    """Send the pending-approval digest if one is due. Returns True if sent.

    Called from the maintenance sweep.  No-op unless digest mode is on,
    an admin address is set, email delivery is configured, the configured
    interval has elapsed since the last digest, and the queue is non-empty.
    The timestamp is only stamped when a digest was actually accepted for
    delivery, so a full queue keeps notifying instead of going silent.
    """
    from datetime import datetime, timezone
    from .db import (
        get_pending_accounts,
        get_approval_notify_email,
        get_approval_notify_mode,
        get_approval_notify_digest_hours,
        get_approval_notify_last_digest_at,
        update_hosting_settings,
    )

    if get_approval_notify_mode() != "digest":
        return False
    admin_email = get_approval_notify_email()
    if not admin_email or not is_configured():
        return False
    moment = now or datetime.now(timezone.utc)
    last = get_approval_notify_last_digest_at()
    if last:
        try:
            previous = datetime.fromisoformat(last)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            elapsed = (moment - previous).total_seconds()
            if elapsed < get_approval_notify_digest_hours() * 3600:
                return False
        except (TypeError, ValueError):
            pass
    pending = get_pending_accounts()
    if not pending:
        return False
    if not notify_admin_signup_digest(admin_email, pending):
        return False
    try:
        update_hosting_settings(approval_notify_last_digest_at=moment.isoformat())
    except Exception as exc:
        _logger.warning("Could not stamp approval digest time: %s", exc)
    return True
