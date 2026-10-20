"""Whether hosted wikis get hard XFS project quotas: ``BW_HOSTING_TENANT_QUOTAS`` in ``config/app.env``.

XFS is never required: every wiki is held to its plan by the application
(upload checks, the portal's storage check every 5 minutes, which suspends a
wiki above 110% of its plan, and ``HOSTING_AGENT_MAX_STORAGE_BYTES`` for a
wiki without a plan). Hard per-wiki byte and inode quotas, enforced by the
kernel, are added where the storage supports them. Three settings, read the
same way by the runtime agent (which starts the wikis), the lifecycle
controller (which must not stop wikis that could not start again) and
``status``:

``auto`` (the default, also for new installations)
    Hard quotas (XFS project quotas) whenever the wiki storage
    (``INSTANCES_DIR``) enforces them. On other storage (ext4, or a root
    agent whose systemd unit cannot use quotas yet) the wikis run without
    them, with a notice (agent log at every start, ``status``, the portal's
    administration page). When quotas were enforced on this platform before
    (its private quota registry exists) and the storage does not enforce
    them now (a remount without ``prjquota``, a replaced volume), the wikis
    still start without them and a warning goes to the agent log,
    ``status`` and the administration page (:attr:`Policy.lost`).
``required``
    Set by the operator: wikis start only under verified hard quotas, and the lifecycle controller refuses an operation before it
    stops anything when they could not start again.
``off``
    Never use project quotas, whatever the storage supports.

Standard library only: the controller runs with the system Python.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VARIABLE = "BW_HOSTING_TENANT_QUOTAS"
MODES = ("auto", "required", "off")
DEFAULT = "auto"
INSTALL_DEFAULT = "auto"  # new hosting installations too: XFS is never required, unless the operator asks
STATE_FILE = "allocations.sqlite3"  # in /var/lib/<service>-quotas: exists once a wiki was put under quota
HOW_TO_ENABLE = ("put the wiki storage (INSTANCES_DIR) on an XFS filesystem mounted with prjquota (UPGRADING.md, "
                 "\"Wiki storage and hard quotas\"): auto then uses them, and 'bananawiki config set tenant-quotas "
                 "required' keeps wikis from ever starting without them")
LIMITS = ("Each wiki is held to its plan only by the application: its uploads are checked, and the portal's storage "
          "check every 5 minutes suspends a wiki above 110% of its plan, so one wiki can exceed its plan, and fill "
          "the disk, in between")


class InvalidSetting(ValueError):
    """``BW_HOSTING_TENANT_QUOTAS`` is not one of :data:`MODES`."""


def setting(environment: Mapping[str, str]) -> str:
    """The configured mode (``auto`` when unset); raises :class:`InvalidSetting` for anything else."""
    value = (environment.get(VARIABLE) or "").strip().lower()
    if not value:
        return DEFAULT
    if value not in MODES:
        raise InvalidSetting(f"{VARIABLE} in config/app.env must be auto, required or off (found {value[:20]!r}).")
    return value


def state_file(state_dir: str | Path) -> Path:
    """The quota registry the runtime agent creates when it first puts a wiki under quota."""
    return Path(state_dir) / STATE_FILE


def registry_exists(state_dir: str | Path) -> bool:
    """Whether quotas were enforced on this platform before (:func:`state_file` exists).

    It tells quotas that were lost (:attr:`Policy.lost`, a warning) from
    storage that never had them (a notice). When that cannot be told (the
    directory cannot be examined, an I/O error), True: a loss is never
    reported as a notice. ``os.lstat``, not ``Path.exists()``,
    which on Python 3.14 answers False for such errors.
    """
    try:
        os.lstat(state_file(state_dir))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class Policy:
    """What the runtime agent does with the wikis' storage.

    ``enforced``: wikis run only under verified XFS project quotas.
    ``refused``: they should (``required``), and the storage cannot, so no
    wiki starts (fail closed). ``lost``: hard quotas were enforced on this
    platform before and the storage does not enforce them now; the wikis start
    without them (``auto``), and :meth:`warning` says so as a warning.
    ``reason``: why quotas are not enforced, or why starts are refused.
    """

    setting: str
    enforced: bool
    reason: str = ""
    refused: bool = False
    lost: bool = False

    @property
    def hard_quotas(self) -> bool:
        return self.enforced and not self.refused

    @property
    def severity(self) -> str | None:
        """How :meth:`warning` is shown: ``error`` (refused), ``warning`` (lost), ``notice``, or None (enforced)."""
        if self.refused:
            return "error"
        if self.lost:
            return "warning"
        return None if self.enforced else "notice"

    def to_dict(self) -> dict[str, Any]:
        return {"setting": self.setting, "hard_quotas": self.hard_quotas, "refused": self.refused,
                "lost": self.lost, "reason": self.reason}

    def warning(self) -> str | None:
        """What an operator must be told (see :attr:`severity`), or None when hard quotas are enforced."""
        if self.refused:
            return f"Hosted wikis cannot start: {self.reason}"
        if self.lost:
            return (f"Hard quotas were enforced on this platform before and are no longer available: {self.reason}. "
                    f"The wikis now start without hard per-wiki storage quotas ({VARIABLE}={self.setting}). "
                    f"{LIMITS}. If this is not intended, restore what changed: the wiki storage's XFS prjquota mount "
                    "(remounted without prjquota, or replaced), or, when 'bananawiki status' says the services run "
                    "under systemd units an older controller wrote, this release's units ('bananawiki restart'). "
                    "Within 5 minutes the maintenance service then puts every running wiki under quota again. Once "
                    "they are back, 'bananawiki config set tenant-quotas required' keeps wikis from ever starting "
                    "without hard quotas.")
        if self.enforced:
            return None
        how = ("run 'bananawiki config set tenant-quotas auto' with the wiki storage on an XFS filesystem mounted "
               "with prjquota" if self.setting == "off" else HOW_TO_ENABLE)
        return (f"Hard per-wiki storage quotas are not enforced ({VARIABLE}={self.setting}: {self.reason}). "
                f"{LIMITS}. For hard limits, {how}.")


def decide(configured: str, *, problem: Callable[[], str | None], enforced_before: Callable[[], bool]) -> Policy:
    """The policy for *configured* (see the module docstring).

    *problem* says why the wiki storage does not enforce XFS project quotas
    (None when it does); *enforced_before* whether quotas were enforced here
    before (:func:`registry_exists`), which only changes what is reported.
    Both are only called when needed.
    """
    if configured == "off":
        return Policy("off", False, f"{VARIABLE}=off turns them off")
    found = problem()
    if found is None:
        return Policy(configured, True)
    if configured == "required":
        return Policy("required", True, f"{VARIABLE}=required, but the wiki storage does not enforce XFS "
                                        f"project quotas: {found}. Put it on XFS mounted with prjquota, or run "
                                        "'bananawiki config set tenant-quotas auto' to run without them.",
                      refused=True)
    return Policy("auto", False, found, lost=enforced_before())
