"""
BananaWiki: Server restart helpers.

Implements the admin-triggered "Restart server" action exposed in
``Site Settings → Advanced``.  The restart is **fully isolated to the
calling wiki**:

* On the **main wiki when supervised by systemd** (the
  ``bananawiki.service`` unit ships with ``Restart=always``) the
  helper sends ``SIGTERM`` to the Gunicorn master and systemd brings
  the unit back.  Sibling wikis on the same host are unaffected
  because their masters are different processes that we never touch.
* On the **main wiki when not under systemd** (e.g. someone ran
  ``./start.sh`` directly, or the wiki is supervised by something
  else that does not auto-respawn on exit) the helper performs a
  Gunicorn graceful re-exec: ``SIGUSR2`` forks a fresh master, and
  the old master is then asked to drain via ``SIGWINCH`` followed by
  ``SIGTERM``.  No supervisor is needed because Gunicorn itself
  fork-replaces the master and the new master keeps serving traffic.
* On a **hosted instance** (each instance has its own daemonised
  Gunicorn master spawned by :mod:`hosting.instance_manager` and is
  uniquely identified by ``BW_INSTANCE_DIR``) the helper performs the
  same graceful re-exec sequence as the non-systemd main wiki case.

A best-effort ``nginx -s reload`` is also attempted on the main wiki
only: hosted instances live behind the hosting platform's shared
nginx, which they intentionally must not touch.

The 60-second cooldown is enforced through
:func:`db.check_and_claim_server_restart` against
``site_settings.last_server_restart_at`` (a per-wiki value).  The
cooldown duration itself lives in :data:`config.SERVER_RESTART_COOLDOWN_SECONDS`:
a hardcoded constant with no UI / DB / env-var override on
purpose, per the feature spec.
"""

import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import config
import db


_logger = logging.getLogger("bananawiki.server_restart")

# Delay before the background restart thread begins signalling, so the
# HTTP response carrying the success flash message has time to flush
# back to the admin's browser.
_RESTART_RESPONSE_FLUSH_DELAY = 0.5

# How long to wait between sending ``SIGUSR2`` (fork a new Gunicorn
# master with fresh code) and asking the old master to drain.  The
# new master needs enough time to spawn workers and bind the port.
_HOSTED_REEXEC_HANDOVER_SECONDS = 5.0


def is_hosted_instance() -> bool:
    """Return ``True`` when the current process is a hosted instance.

    Hosted instances are spawned by :func:`hosting.instance_manager._start_process`
    with ``BW_INSTANCE_DIR`` pointing at the per-instance data directory
    (see :func:`hosting.instance_manager._instance_env`).  The main wiki
    does not set this variable, so its presence is a reliable signal.
    """
    return bool(os.environ.get("BW_INSTANCE_DIR"))


def is_easy_wiki() -> bool:
    """Return ``True`` when the current process is in EasyWiki mode.

    EasyWiki is a minimal-feature mode activated from the hosting platform.
    The hosting platform injects ``BW_EASY_WIKI=1`` into the instance
    environment when the ``easy_wiki`` flag is set on the instance row.
    Standalone deployments (``app.py``) never set this variable.
    """
    return os.environ.get("BW_EASY_WIKI", "0") not in ("0", "false", "no", "")


# Name of the systemd unit shipped by ``install.sh``.  This is the only
# service whose ``Restart=always`` directive is guaranteed to bring the
# wiki back after a plain ``SIGTERM``; anything else has to use a
# different strategy so traffic keeps flowing.
_BANANAWIKI_SERVICE_UNIT = "bananawiki.service"

# ``deploy.sh`` writes one systemd unit per managed site, named
# ``bananawiki-<slug>.service``.  Those units default to
# ``Restart=on-failure`` (a clean SIGTERM exit therefore won't trigger
# a restart) and inherit systemd's default ``KillMode=control-group``
# (so when the MainPID exits the rest of the cgroup is SIGKILL'd,
# including any SIGUSR2-forked successor master).  A separate detection
# function picks those out so the restart helper can fall back to a
# SIGHUP-only refresh that leaves the MainPID alive.
#
# ``bananawiki-hosting.service`` is explicitly excluded because *its*
# MainPID is the hosting Flask app, not a wiki.  Hosted instances
# daemonized under it run in the hosting cgroup but are not its
# MainPID, so their masters can be replaced via the normal graceful
# re-exec without disturbing the hosting service.
_BWIKI_HOSTING_UNIT = "bananawiki-hosting.service"
_SITE_UNIT_RE = re.compile(r"/bananawiki-[^/]+\.service\b")
_HOSTING_UNIT_RE = re.compile(r"/bananawiki-hosting\.service\b")


def _read_master_cgroup(master_pid: int) -> str:
    """Return ``/proc/<master_pid>/cgroup`` contents or empty string.

    Returns ``""`` on any error (file missing, permission denied,
    decode error, …).  Callers must treat the empty string as
    "supervision unknown" and fall back to the safe graceful re-exec
    path.
    """
    if master_pid <= 0:
        return ""
    try:
        with open(f"/proc/{master_pid}/cgroup", "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def is_under_systemd(master_pid: int = 0) -> bool:
    """Return ``True`` when the Gunicorn master is directly supervised
    by the ``bananawiki.service`` systemd unit.

    A plain ``SIGTERM`` is only safe to deliver to the Gunicorn master
    when systemd's ``Restart=always`` on **this exact service** is
    going to bring it back.  We therefore look at the master's cgroup
    file rather than the worker's environment: a process placed in
    ``/system.slice/bananawiki.service`` was started by that unit and
    will be respawned by it; anything else (login shell, ``sshd``, the
    hosting platform, another wrapper service, …) is not guaranteed
    to.

    We deliberately do **not** trust the ``INVOCATION_ID`` /
    ``JOURNAL_STREAM`` environment variables: those are inherited
    through ``fork()`` from any systemd-supervised parent and are
    therefore positive even when *we* are not the unit systemd is
    going to restart.  That false positive was the reason the restart
    button left the wiki down on installs where ``./start.sh`` was
    launched from inside another systemd-managed process tree (sshd,
    the hosting portal, container init, the dev sandbox, …).

    ``master_pid`` may be passed in by callers that already resolved
    it; when omitted we fall back to :func:`_gunicorn_master_pid`.
    """
    if master_pid <= 0:
        master_pid = _gunicorn_master_pid()
    cgroup = _read_master_cgroup(master_pid)
    if not cgroup:
        return False
    # cgroup file lines look like:
    #   0::/system.slice/bananawiki.service          (cgroupv2)
    #   12:memory:/system.slice/bananawiki.service   (cgroupv1)
    # Match the unit name as a path segment (preceded by ``/``) so a
    # service called e.g. ``not-bananawiki.service`` cannot trigger a
    # false positive.
    return f"/{_BANANAWIKI_SERVICE_UNIT}" in cgroup


def get_restart_cooldown_remaining() -> int:
    """Return seconds remaining on the per-wiki restart cooldown.

    Returns ``0`` when a restart is currently allowed.  The value is
    rounded **up** so the user-visible "wait N seconds" message never
    promises a moment that has already passed.
    """
    last_str = db.get_last_server_restart_at()
    if not last_str:
        return 0
    try:
        last_dt = datetime.fromisoformat(str(last_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    remaining = config.SERVER_RESTART_COOLDOWN_SECONDS - elapsed
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining))


def _gunicorn_master_pid() -> int:
    """Return the PID of the Gunicorn master that owns this process.

    Returns ``0`` when no usable master can be identified, e.g. when
    the wiki is run directly via ``python app.py`` for development, or
    when the parent has already exited and been re-parented to ``init``.
    Callers must treat ``0`` as "do nothing", never as a wildcard.
    """
    try:
        ppid = os.getppid()
    except OSError:
        return 0
    if ppid <= 1:
        # Re-parented to init: the original Gunicorn master is gone, or
        # we are not running under Gunicorn at all (dev server).  Either
        # way there is nothing safe to signal.
        return 0
    return ppid


def _attempt_nginx_reload() -> None:
    """Best-effort ``nginx -s reload``.

    Used only on the main wiki.  Failures are silently swallowed: nginx
    may not be on ``$PATH`` for the unprivileged Gunicorn user, the
    binary may not exist on minimal containers, or the reload may be
    rejected because the user lacks permission to talk to the master
    socket.  None of those are fatal. The wiki was asked to restart
    its *own* stack, and the gunicorn restart below is what actually
    delivers the user-visible refresh.
    """
    try:
        subprocess.run(
            ["nginx", "-s", "reload"],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        # Intentionally swallowed: see docstring.
        pass


def is_under_supervised_systemd_unit(master_pid: int = 0) -> bool:
    """Return ``True`` when the Gunicorn master is the MainPID of a
    per-site ``bananawiki-<slug>.service`` unit written by
    ``deploy.sh``.

    These units default to ``Restart=on-failure`` and the systemd
    default ``KillMode=control-group``.  Sending ``SIGTERM`` to such a
    master would therefore:

    1. Clean-exit the MainPID (signal in the "clean exit" set), which
       does **not** trigger ``Restart=on-failure``.
    2. Cause systemd to SIGKILL the rest of the cgroup, taking down
       any ``SIGUSR2``-forked successor master before it can finish
       binding the listening socket.

    Callers in this state must use a SIGHUP-only refresh that leaves
    the MainPID intact, see :func:`_restart_via_sighup`.

    ``bananawiki-hosting.service`` is excluded because its MainPID is
    the hosting Flask app, not a wiki; hosted instances daemonized
    under it are *not* the unit's MainPID and can safely use the
    graceful re-exec sequence.
    """
    if master_pid <= 0:
        master_pid = _gunicorn_master_pid()
    cgroup = _read_master_cgroup(master_pid)
    if not cgroup:
        return False
    managed_unit = os.environ.get("BW_SYSTEMD_SERVICE", "")
    if re.fullmatch(r"[a-z][a-z0-9-]{1,45}\.service", managed_unit):
        if re.search(r"/" + re.escape(managed_unit) + r"(?:\n|$)", cgroup):
            return True
    if _HOSTING_UNIT_RE.search(cgroup):
        return False
    return bool(_SITE_UNIT_RE.search(cgroup))


def _restart_via_sighup(master_pid: int) -> None:
    """Refresh Gunicorn workers via a single ``SIGHUP`` to the master.

    Used when the master is the MainPID of a ``bananawiki-<slug>.
    service`` unit (see :func:`is_under_supervised_systemd_unit`).
    ``SIGHUP`` tells Gunicorn to gracefully spawn fresh workers and
    shut the old ones down without exiting the master itself, so
    systemd never sees the MainPID change, never tears down the
    cgroup, and never has to decide whether to restart us.  This is
    the only strategy that keeps these wikis responsive after the
    admin "Restart server" button is clicked.
    """
    if sys.platform == "win32":
        _logger.warning("SIGHUP restart is not supported on Windows")
        return
    try:
        os.kill(master_pid, signal.SIGHUP)
    except ProcessLookupError:
        _logger.warning(
            "Gunicorn master PID %d disappeared before SIGHUP could be sent",
            master_pid,
        )
    except PermissionError:
        _logger.exception(
            "Permission denied signalling Gunicorn master PID %d for restart",
            master_pid,
        )


def _restart_main_wiki(master_pid: int) -> None:
    """Restart the main-wiki Gunicorn stack.

    Strategy depends on how the master is supervised:

    * **Under ``bananawiki.service`` (install.sh)**: send a single
      ``SIGTERM``.  ``Restart=always`` brings the whole process tree
      back automatically.
    * **Under ``bananawiki-<slug>.service`` (deploy.sh per-site
      unit)**: send a single ``SIGHUP``.  The MainPID stays alive
      (so neither ``Restart=on-failure`` nor ``KillMode=control-group``
      tears anything down) while Gunicorn cycles its workers.
    * **Not under any wiki systemd unit**: perform the graceful
      Gunicorn re-exec used for hosted instances: ``SIGUSR2`` forks a
      new master with the current code, the new master binds the same
      socket, and the old master then drains and exits.  This keeps
      the wiki up even without any external supervisor (e.g. when run
      via ``./start.sh`` from a shell).
    """
    if sys.platform == "win32":
        _logger.warning("Server restart via signals is not supported on Windows")
        return
    _attempt_nginx_reload()
    if is_under_systemd(master_pid):
        try:
            os.kill(master_pid, signal.SIGTERM)
        except ProcessLookupError:
            _logger.warning(
                "Gunicorn master PID %d disappeared before SIGTERM could be sent",
                master_pid,
            )
        except PermissionError:
            _logger.exception(
                "Permission denied signalling Gunicorn master PID %d for restart",
                master_pid,
            )
        return
    if is_under_supervised_systemd_unit(master_pid):
        _logger.info(
            "Master PID %d is under a bananawiki-<slug>.service unit; "
            "using SIGHUP-only refresh to keep MainPID alive",
            master_pid,
        )
        _restart_via_sighup(master_pid)
        return
    _logger.info(
        "No %s supervision detected for main wiki "
        "(master PID %d cgroup did not match); using graceful re-exec",
        _BANANAWIKI_SERVICE_UNIT, master_pid,
    )
    _restart_hosted_instance(master_pid)


def _restart_hosted_instance(master_pid: int) -> None:
    """Gracefully re-exec the Gunicorn master of a hosted instance.

    If the master is also the MainPID of a ``bananawiki-<slug>.
    service`` unit (the deploy.sh per-site case, which sets
    ``BW_INSTANCE_DIR`` so callers route us here even though we are
    really a systemd-supervised wiki), the SIGTERM at the end of the
    sequence would kill the unit's MainPID, the default
    ``KillMode=control-group`` would then SIGKILL the SIGUSR2-forked
    successor master before it could take over, and the unit's
    ``Restart=on-failure`` would *not* respawn us, leaving the wiki
    down.  We detect that case explicitly and fall back to a
    SIGHUP-only refresh that keeps the MainPID alive.

    Hosting-platform-spawned instances (detected via
    :func:`is_hosted_instance`) also take the SIGHUP-only path so the
    hosting process manager never loses track of the master process.

    Otherwise (standalone ``./start.sh`` from a shell, dev sandbox, …)
    the sequence is:

    1. ``SIGUSR2``: Gunicorn forks a new master process tree with the
       current code.  The new master spawns its own workers and binds
       the same listening socket.
    2. Sleep :data:`_HOSTED_REEXEC_HANDOVER_SECONDS` so the new workers
       are ready to accept traffic.
    3. ``SIGWINCH``: old master gracefully drains its workers.
    4. ``SIGTERM``: old master itself exits cleanly.

    Each step independently swallows ``ProcessLookupError`` so the
    restart still succeeds even if the old master happens to disappear
    earlier than expected.
    """
    if is_under_supervised_systemd_unit(master_pid) or is_hosted_instance():
        _logger.info(
            "Master PID %d is under systemd supervision or a hosted instance; "
            "using SIGHUP-only refresh to keep the master alive",
            master_pid,
        )
        _restart_via_sighup(master_pid)
        return
    if sys.platform == "win32":
        _logger.warning("Graceful re-exec is not supported on Windows")
        return
    for sig, label, sleep_after in (
        (signal.SIGUSR2, "SIGUSR2 (re-exec)", _HOSTED_REEXEC_HANDOVER_SECONDS),
        (signal.SIGWINCH, "SIGWINCH (drain workers)", 1.0),
        (signal.SIGTERM, "SIGTERM (exit old master)", 0.0),
    ):
        try:
            os.kill(master_pid, sig)
        except ProcessLookupError:
            _logger.info(
                "Gunicorn master PID %d already gone before %s: restart already in progress",
                master_pid, label,
            )
            return
        except PermissionError:
            _logger.exception(
                "Permission denied sending %s to Gunicorn master PID %d",
                label, master_pid,
            )
            return
        if sleep_after > 0:
            time.sleep(sleep_after)


def _run_restart_in_background(master_pid: int, hosted: bool) -> None:
    """Background-thread body that performs the actual signalling.

    Runs after a short delay so the calling request can finish writing
    its HTTP response before the worker is asked to shut down.
    """
    time.sleep(_RESTART_RESPONSE_FLUSH_DELAY)
    try:
        if hosted:
            _restart_hosted_instance(master_pid)
        else:
            _restart_main_wiki(master_pid)
    except Exception:  # pragma: no cover - defensive; logged not raised
        _logger.exception("Unexpected error during background server restart")


def trigger_server_restart():
    """Schedule a restart of *this wiki's* Gunicorn stack.

    Returns ``(True, "")`` when the background restart thread has been
    started, or ``(False, reason)`` when the restart cannot proceed
    (e.g. running outside Gunicorn).  The actual signalling happens
    asynchronously so the calling HTTP request can flush its response.

    This function never touches sibling wikis on the same host. It
    only signals the Gunicorn master that is the **direct parent** of
    the current worker process.
    """
    master_pid = _gunicorn_master_pid()
    if master_pid <= 0:
        return False, (
            "No Gunicorn master process detected. The restart action only "
            "works when BananaWiki is running under Gunicorn."
        )

    hosted = is_hosted_instance()
    thread = threading.Thread(
        target=_run_restart_in_background,
        args=(master_pid, hosted),
        name="bananawiki-server-restart",
        daemon=True,
    )
    thread.start()
    _logger.info(
        "Scheduled server restart (mode=%s, master_pid=%d)",
        "hosted-instance" if hosted else "main-wiki",
        master_pid,
    )
    return True, ""
