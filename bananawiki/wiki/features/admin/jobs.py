"""Background jobs of the administration feature."""

from __future__ import annotations

import logging
from datetime import timedelta

from ....core.timeutil import parse, to_sql, utcnow
from ... import settings
from ...db import db
from ...templating import site_timezone
from . import service

log = logging.getLogger("bananawiki.admin")

AUTO_LOGOUT_JOB = "admin.auto_logout"


def account_cleanup() -> None:
    """Lift finished suspensions and delete sign-ups that were denied or never reviewed in time."""
    lifted = service.expire_suspensions()
    deleted = service.delete_expired_signups(
        int(settings.get("approval_denied_timeout_hours", 24) or 0),
        int(settings.get("approval_pending_timeout_hours", 0) or 0),
    )
    if lifted or deleted:
        log.info("Account clean-up: %d suspension(s) lifted, %d sign-up(s) deleted", lifted, deleted)


def last_logout_moment(hour: int):
    """The most recent moment the clock showed *hour*:00 in the site time zone (as UTC)."""
    local_now = utcnow().astimezone(site_timezone())
    moment = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if moment > local_now:
        moment -= timedelta(days=1)
    return moment


def auto_logout() -> None:
    """Daily sign-out of everyone at ``auto_logout_hour`` (site time zone).

    The job runs every few minutes. It acts when the configured hour passed
    since its previous run and then revokes only the sessions that started
    before that moment, so it is idempotent, safe with several workers, and
    catches up after downtime without signing out people who logged in later.
    """
    if not settings.get("auto_logout_enabled"):
        return
    hour = min(max(int(settings.get("auto_logout_hour", 0) or 0), 0), 23)
    previous = parse(db.scalar("SELECT last_run_at FROM job_runs WHERE name = ?", (AUTO_LOGOUT_JOB,)))
    moment = last_logout_moment(hour)
    if previous is None or previous >= moment:
        return
    cutoff = to_sql(moment)
    count = db.execute(
        "UPDATE user_sessions SET revoked_at = ? WHERE revoked_at IS NULL AND created_at < ?",
        (to_sql(utcnow()), cutoff),
    ).rowcount
    log.info("Scheduled sign-out ended %d session(s)", count)
