"""The hosting maintenance service (``python -m hosting.maintenance --interval 300``).

Runs outside the web workers so reloads never duplicate or skip
retention-critical work. At start it asks the runtime to bring back every
wiki the database says is running (the 1.4 updater removes all tenant
containers and waits for them to return). Then, every *interval* seconds,
:func:`run_once` performs one pass:

* lift timed suspensions of wikis and accounts that have run out;
* terminate expired wikis (they enter the grace period) and delete data
  whose grace period ended;
* suspend non-admin wikis that materially exceed their storage cap;
* renew custom-domain proofs;
* delete denied accounts past the configured timeout and accounts whose
  scheduled deletion is due (their wikis are terminated first);
* email administrators about requests waiting for them (immediately, as a
  digest or daily, see :mod:`.notifications`) and owners about decisions on
  their own requests; purge deleted-account tombstones and old notices;
* prune expired sessions, rate-limit hits, OAuth grants and stale uploads;
* publish the routing table.

A runtime may also define ``maintenance_tick()``; it is called at the end of
every pass for runtime-side periodic work (scheduled Google Drive backups,
pruning 1.4 log files and interrupted imports).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Flask

from ..core.timeutil import now_sql, sql_in
from . import accounts, attention, domains, instances, notifications
from .db import connection_scope, db

log = logging.getLogger("bananawiki.hosting.maintenance")
_stop = threading.Event()


def _delete_account(account_id: str) -> None:
    for inst in instances.owned_by(account_id):
        instances.terminate(inst, actor_id=None, reason="account_deleted")
    accounts.delete(account_id)


def _prune() -> None:
    db.execute("DELETE FROM hosting_rate_limit_hits WHERE hit_at < ?", (sql_in(days=-1),))
    db.execute("DELETE FROM hosting_login_attempts WHERE attempted_at < ?", (sql_in(days=-1),))
    db.execute("DELETE FROM hosting_account_sessions WHERE expires_at < ? OR revoked_at < ?",
               (sql_in(days=-30), sql_in(days=-30)))
    db.execute("DELETE FROM hosting_oauth_authorization_codes WHERE expires_at < ?", (now_sql(),))
    db.execute("DELETE FROM hosting_oauth_access_tokens WHERE expires_at < ?", (now_sql(),))


def _prune_uploads(app: Flask) -> None:
    cutoff = time.time() - 24 * 3600
    for root in (app.config["HOSTING"].archives.import_temp_dir, app.config["HOSTING"].archives.export_temp_dir):
        directory = Path(root)
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                if not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()
            except OSError:
                continue


def _steps(app: Flask) -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("instance suspensions", instances.lift_expired_suspensions),
        ("account suspensions", lambda: [accounts.unsuspend(i, actor_id=None, automatic=True)
                                         for i in accounts.expired_suspensions()]),
        ("expired wikis", instances.terminate_expired),
        ("grace periods", instances.purge_expired_grace_periods),
        ("storage quotas", instances.enforce_storage_quotas),
        ("custom domains", domains.refresh),
        ("denied accounts", lambda: [_delete_account(i) for i in accounts.expired_denials()]),
        ("scheduled deletions", lambda: [_delete_account(i) for i in accounts.due_deletions()]),
        ("attention emails", notifications.send_attention),
        ("decision emails", notifications.send_decisions),
        ("tombstones", accounts.purge_tombstones),
        ("pruning", _prune),
        ("notices", attention.prune),
        ("uploads", lambda: _prune_uploads(app)),
        ("routes", instances.sync_routes),
    ]


def run_once(app: Flask) -> dict[str, Any]:
    """One maintenance pass; each step is isolated so one failure never stops the rest."""
    results: dict[str, Any] = {}
    with app.test_request_context("/"), connection_scope(app.extensions["bananawiki.hosting.database"]):
        for name, step in _steps(app):
            if _stop.is_set():
                break
            try:
                results[name] = step()
            except Exception as error:  # noqa: BLE001 - keep the service alive
                log.exception("Maintenance step %s failed: %s", name, error)
                results[name] = "failed"
        tick = getattr(app.extensions["bananawiki.hosting.runtime"], "maintenance_tick", None)
        if callable(tick) and not _stop.is_set():
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("Runtime maintenance failed")
    return results


def recover(app: Flask) -> int:
    """Start every wiki marked running (after an update or a host reboot), then publish their routes.

    The updater's readiness check waits for both (audit C10): each running
    wiki healthy on its container address and routed by Caddy, so the routes
    go out now rather than at the end of the first, slower pass.
    """
    with app.test_request_context("/"), connection_scope(app.extensions["bananawiki.hosting.database"]):
        rows = db.all("SELECT * FROM instances WHERE status = 'running'")
        specs = [instances.spec(row) for row in rows]
        started = app.extensions["bananawiki.hosting.runtime"].recover(specs)
        instances.sync_routes()
        return started


def _request_stop(_signum: int, _frame: Any) -> None:
    _stop.set()


def run(app: Flask, *, interval: int = 300, once: bool = False) -> int:
    """Recover the running wikis, then one pass every *interval* seconds until SIGTERM/SIGINT.

    A signal ends the current pass after its current step and stops the loop.
    """
    if not once:
        try:
            log.info("Recovered %d wiki(s)", recover(app))
        except Exception:  # noqa: BLE001
            log.exception("Recovering running wikis failed")
    while not _stop.is_set():
        run_once(app)
        if once or _stop.wait(max(30, int(interval))):
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BananaWiki Hosting maintenance service")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes (minimum 30)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    from .app import create_app

    return run(create_app(), interval=args.interval, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
