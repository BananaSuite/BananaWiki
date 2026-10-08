"""Features, events, template slots and background jobs.

A *feature* is a subpackage of :mod:`bananawiki.wiki.features` whose
``__init__.py`` defines ``FEATURE = Feature(...)``. The application discovers
them at start-up, registers their blueprints, translations, event handlers,
slots and jobs. Whether a feature is switched on is decided in one place,
:func:`is_enabled`, which routes, templates, permissions and jobs all use:

* ``toggle="always"`` - core functionality, cannot be switched off;
* ``toggle="plugin"`` - a row in the ``plugins`` table (the 1.4 "built-in
  plugins"), so existing on/off choices survive the upgrade;
* ``toggle="setting"`` - a boolean ``site_settings`` column.

The operator can additionally hide features with ``BW_EASY_WIKI`` or the
managed-hosting deny list. Disabling a feature never deletes its data or
rewrites its settings.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from flask import Blueprint, abort, current_app, g, has_request_context, jsonify, request

log = logging.getLogger("bananawiki.features")


@dataclass(frozen=True)
class NavItem:
    """A link shown in the sidebar (``area="apps"``), user menu or admin menu."""

    label: str  # translation key
    endpoint: str
    icon: str = "circle"
    area: Literal["apps", "user", "admin"] = "apps"
    order: int = 100
    # Returns True when the current user should see the link.
    visible: Callable[[Any], bool] | None = None
    badge: Callable[[Any], int] | None = None


@dataclass(frozen=True)
class Job:
    """A periodic task. Exactly one worker process runs each job per interval."""

    name: str
    interval: int  # seconds
    run: Callable[[], Any]
    initial_delay: int = 30


@dataclass(frozen=True)
class AttentionSource:
    """A queue of items waiting for someone's decision (see :mod:`bananawiki.wiki.attention`).

    ``count(user)`` must be permission-aware (0 for people who may not act on
    the queue) and cheap: one indexed ``COUNT``. ``oldest(user)`` optionally
    returns the timestamp of the item waiting longest. ``audience`` is the
    lowest role that can ever act on the queue ("admin" or "editor"); it lets
    the email job skip everyone else without calling ``count``.
    """

    id: str
    label: str  # translation key
    endpoint: str
    count: Callable[[Any], int]
    endpoint_args: dict[str, Any] = field(default_factory=dict)
    oldest: Callable[[Any], str | None] | None = None
    audience: Literal["admin", "editor"] = "admin"
    order: int = 100


@dataclass
class Feature:
    id: str
    name: str  # translation key
    description: str = ""  # translation key
    toggle: Literal["always", "plugin", "setting"] = "plugin"
    setting: str | None = None  # site_settings column when toggle == "setting"
    default_enabled: bool = True
    easy_wiki: bool = True  # available when BW_EASY_WIKI is set
    blueprints: list[Blueprint] = field(default_factory=list)
    nav: list[NavItem] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    events: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    slots: dict[str, Callable[..., str]] = field(default_factory=dict)
    interceptors: dict[str, Callable[..., Any]] = field(default_factory=dict)
    attention: list[AttentionSource] = field(default_factory=list)
    init_app: Callable[[Any], None] | None = None
    version: str = "1.6.0"
    order: int = 100
    package_dir: Path | None = None


class Registry:
    def __init__(self) -> None:
        self.features: dict[str, Feature] = {}
        self._handlers: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._slots: dict[str, list[tuple[str, Callable[..., str]]]] = {}
        self._interceptors: dict[str, list[tuple[str, Callable[..., Any]]]] = {}

    def add(self, feature: Feature) -> None:
        if feature.id in self.features:
            raise ValueError(f"Duplicate feature id {feature.id!r}")
        self.features[feature.id] = feature
        for event, handlers in feature.events.items():
            for handler in handlers:
                self._handlers.setdefault(event, []).append((feature.id, handler))
        for slot, render in feature.slots.items():
            self._slots.setdefault(slot, []).append((feature.id, render))
        for point, handler in feature.interceptors.items():
            handlers = self._interceptors.setdefault(point, [])
            handlers.append((feature.id, handler))
            # The first answer wins, so interceptors run in feature order.
            handlers.sort(key=lambda item: (self.features[item[0]].order, item[0]))

    def ordered(self) -> list[Feature]:
        return sorted(self.features.values(), key=lambda f: (f.order, f.id))


def registry() -> Registry:
    return current_app.extensions["bananawiki.registry"]


# ── Feature switches ──────────────────────────────────────────────────────────


def _plugin_states() -> dict[str, bool]:
    from .db import db

    if has_request_context():
        cached = g.get("_plugin_states")
        if cached is not None:
            return cached
    rows = db.all("SELECT id, enabled FROM plugins")
    states = {row["id"]: bool(row["enabled"]) for row in rows}
    if has_request_context():
        g._plugin_states = states
    return states


def is_enabled(feature_id: str) -> bool:
    feature = registry().features.get(feature_id)
    if feature is None:
        return False
    cfg = current_app.config["BW"]
    if cfg.easy_wiki and not feature.easy_wiki:
        return False
    if feature_id in cfg.managed_plugin_denylist:
        return False
    if feature.toggle == "always":
        return True
    if feature.toggle == "setting":
        from . import settings

        return bool(settings.get(feature.setting or f"{feature_id}_enabled", feature.default_enabled))
    return _plugin_states().get(feature_id, feature.default_enabled)


def set_enabled(feature_id: str, enabled: bool) -> None:
    from ..core.timeutil import now_sql
    from . import settings
    from .db import db

    feature = registry().features[feature_id]
    if feature.toggle == "always":
        raise ValueError(f"{feature_id} cannot be switched off")
    if feature.toggle == "setting":
        settings.update({feature.setting or f"{feature_id}_enabled": 1 if enabled else 0})
        return
    now = now_sql()
    db.execute(
        "INSERT INTO plugins (id, name, version, builtin, enabled, installed_at, enabled_at, disabled_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled, "
        "enabled_at = CASE WHEN excluded.enabled THEN excluded.enabled_at ELSE plugins.enabled_at END, "
        "disabled_at = CASE WHEN excluded.enabled THEN plugins.disabled_at ELSE excluded.disabled_at END",
        (feature_id, feature.name, feature.version, 1 if enabled else 0, now,
         now if enabled else None, None if enabled else now),
    )
    if has_request_context():
        g.pop("_plugin_states", None)


def require_feature(feature_id: str) -> Callable[[], Any]:
    """``before_request`` hook for a feature blueprint: 404 while switched off."""

    def check():
        if not is_enabled(feature_id):
            if request.path.startswith("/api/"):
                return jsonify({"error": "This feature is not enabled."}), 404
            abort(404)
        return None

    return check


def feature_blueprint(feature_id: str, name: str, import_name: str, **kwargs: Any) -> Blueprint:
    """A blueprint that is only reachable while *feature_id* is enabled."""
    bp = Blueprint(name, import_name, **kwargs)
    bp.before_request(require_feature(feature_id))
    return bp


# ── Events ────────────────────────────────────────────────────────────────────


def emit(event: str, **payload: Any) -> None:
    """Call every handler registered for *event* by an enabled feature.

    Events are emitted by services after their transaction commits. A failing
    handler is logged and never breaks the caller.
    Known events: ``page.created``, ``page.updated``, ``page.deleted``,
    ``page.restored``, ``user.created``, ``user.deleted``, ``user.renamed``,
    ``user.name_released``, ``user.login``, ``category.deleted``, ``kanban.board.created``,
    ``kanban.board.updated``, ``kanban.board.deleted``, ``kanban.ticket.created``,
    ``kanban.ticket.updated``, ``kanban.ticket.moved``, ``kanban.ticket.deleted``,
    ``kanban.comment.created``, ``canvas.created``, ``canvas.updated``,
    ``canvas.deleted``.
    """
    for feature_id, handler in registry()._handlers.get(event, []):
        if not is_enabled(feature_id):
            continue
        try:
            handler(**payload)
        except Exception:  # noqa: BLE001 - handlers must not break the emitter
            log.exception("Handler for %s in feature %s failed", event, feature_id)


def intercept(point: str, **context: Any) -> Any:
    """Let enabled features take over an action; return the first non-None answer.

    Unlike events, interceptors run *before* the action and may change its
    outcome (a Flask response, an error translation key, …). Errors propagate:
    an interceptor that fails must not silently allow the action.
    Known points are listed in ARCHITECTURE.md.
    """
    for feature_id, handler in registry()._interceptors.get(point, []):
        if not is_enabled(feature_id):
            continue
        result = handler(**context)
        if result is not None:
            return result
    return None


def prepare(point: str, **context: Any) -> None:
    """Run the interceptors of *point* of every built-in feature, switched off or not, and
    of enabled external plugins; answers are ignored.

    For points where features look after their stored data before a core
    action changes it (``user.delete``): switching a built-in feature off
    keeps its data, so it must stay intact. A switched-off plugin is left
    out, as after a restart, when it is not loaded at all. Errors are logged
    with the feature's id, propagate and stop the action.
    """
    from .plugins_external import runtime

    plugins = set(runtime().loaded_ids())
    for feature_id, handler in registry()._interceptors.get(point, []):
        if feature_id in plugins and not is_enabled(feature_id):
            continue
        try:
            handler(**context)
        except Exception:
            log.exception("Interceptor for %s in feature %s failed", point, feature_id)
            raise


def render_slot(slot: str, **context: Any) -> str:
    """HTML contributed by enabled features to a named template slot."""
    from markupsafe import Markup

    parts = []
    for feature_id, render in registry()._slots.get(slot, []):
        if not is_enabled(feature_id):
            continue
        try:
            html = render(**context)
        except Exception:  # noqa: BLE001
            log.exception("Slot %s in feature %s failed", slot, feature_id)
            continue
        if html:
            parts.append(str(html))
    return Markup("".join(parts))


# ── Background jobs ───────────────────────────────────────────────────────────


class Scheduler:
    """Runs registered jobs in one daemon thread per process.

    A job only runs when this process wins its lease in ``job_runs``, so with
    several Gunicorn workers each job still runs once per interval. The same
    jobs can be run from cron with ``bananawiki jobs run``.
    """

    TICK = 15

    def __init__(self, app: Any):
        self.app = app
        self.owner = f"{threading.get_native_id()}-{id(self)}-{time.time_ns()}"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._lock = threading.Lock()

    def jobs(self) -> list[tuple[Feature, Job]]:
        reg: Registry = self.app.extensions["bananawiki.registry"]
        return [(feature, job) for feature in reg.ordered() for job in feature.jobs]

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="bananawiki-jobs", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK):
            try:
                self.run_due()
            except Exception:  # noqa: BLE001
                log.exception("Job scheduler tick failed")

    def run_due(self, *, force: bool = False, only: str | None = None) -> list[str]:
        from ..core.timeutil import now_sql, sql_in
        from .db import connection_scope

        ran = []
        elapsed = time.monotonic() - self._started_at
        with self.app.app_context(), connection_scope() as session:
            for feature, job in self.jobs():
                if only and job.name != only:
                    continue
                if not force and elapsed < job.initial_delay:
                    continue
                if not is_enabled(feature.id):
                    continue
                with session.transaction():
                    row = session.one("SELECT lease_until, last_run_at FROM job_runs WHERE name = ?", (job.name,))
                    now = now_sql()
                    if row and not force:
                        if row["lease_until"] and row["lease_until"] > now:
                            continue
                        if row["last_run_at"] and row["last_run_at"] > sql_in(seconds=-job.interval):
                            continue
                    session.execute(
                        "INSERT INTO job_runs (name, held_by, lease_until) VALUES (?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET held_by = excluded.held_by, lease_until = excluded.lease_until",
                        (job.name, self.owner, sql_in(seconds=max(300, job.interval))),
                    )
                status, error = "ok", None
                try:
                    job.run()
                except Exception as exc:  # noqa: BLE001
                    status, error = "error", "".join(traceback.format_exception_only(type(exc), exc)).strip()
                    log.exception("Job %s failed", job.name)
                session.execute(
                    "UPDATE job_runs SET held_by = NULL, lease_until = NULL, last_run_at = ?, last_status = ?, "
                    "last_error = ? WHERE name = ?",
                    (now_sql(), status, error, job.name),
                )
                ran.append(job.name)
        return ran
