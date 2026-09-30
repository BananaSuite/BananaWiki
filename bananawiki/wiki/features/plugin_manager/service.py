"""What the plugin admin pages show and change.

Three kinds of entries share one list:

* ``feature``  - a built-in feature; switched with :func:`registry.set_enabled`
  (never touching its settings);
* ``external`` - a third-party plugin: its ``plugins`` row decides whether it
  is loaded at the next start;
* ``retired``  - a ``plugins`` row left by a 1.4 plugin that no longer exists.
  It is inert and can only be removed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import current_app, g, request

from ....core import passwords
from ....core.timeutil import now_sql
from ... import auth, i18n, plugins_external, registry, settings
from ...db import db

SNAPSHOT_DIR = "plugin_safety_snapshots"
SNAPSHOT_KEEP = 5
_SNAPSHOT_NAME = re.compile(r"^\d{8}T\d{6}Z-[A-Za-z0-9_-]+(?:-\d+)?\.db$")
MAX_ORDER_LENGTH = 500

# 1.4 sidebar order keys that name a part of a 1.6 feature.
LEGACY_APP_KEYS = {"chats": "chat", "groups": "chat", "reservations": "page_governance"}


@dataclass
class Entry:
    id: str
    kind: str  # feature | external | retired
    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    toggle: str = "plugin"  # always | plugin | setting (features); plugin for external
    enabled: bool = False  # in effect right now
    stored: bool = False  # the saved switch
    lock: str = ""  # translation key: why the switch cannot be turned on
    loaded: bool = False
    restart: str = ""  # "enable" / "disable" when a restart is needed to finish the change
    error_key: str = ""
    error_values: dict[str, str] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    required_by: list[str] = field(default_factory=list)
    has_files: bool = False
    legacy: bool = False
    path: Path | None = None
    easy_wiki_hidden: bool = False

    @property
    def can_enable(self) -> bool:
        return self.kind != "retired" and self.toggle != "always" and not self.stored and not self.lock \
            and not self.error_key

    @property
    def can_disable(self) -> bool:
        if self.kind == "external":
            return self.stored
        return self.kind == "feature" and self.toggle != "always" and self.stored and not self.easy_wiki_hidden

    @property
    def removable(self) -> bool:
        return self.kind in ("external", "retired")


def _cfg() -> Any:
    return current_app.config["BW"]


def _rows() -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in db.all("SELECT * FROM plugins ORDER BY id")}


def _denied(plugin_id: str) -> bool:
    return plugins_external.runtime().denied(plugin_id)


def _feature_entry(feature: registry.Feature, rows: dict[str, dict[str, Any]]) -> Entry:
    if feature.toggle == "always":
        stored = True
    elif feature.toggle == "setting":
        stored = bool(settings.get(feature.setting or f"{feature.id}_enabled", feature.default_enabled))
    else:
        row = rows.get(feature.id)
        stored = bool(row["enabled"]) if row else feature.default_enabled
    hidden = _cfg().easy_wiki and not feature.easy_wiki
    lock = ""
    if hidden:
        lock = "plugin_manager.lock.easy_wiki"
    elif _denied(feature.id):
        lock = "plugin_manager.lock.denylisted"
    elif feature.toggle == "always":
        lock = "plugin_manager.lock.always"
    return Entry(
        id=feature.id, kind="feature", name=i18n.t(feature.name), description=i18n.t(feature.description, ""),
        version=feature.version, author="BananaWiki", toggle=feature.toggle, enabled=registry.is_enabled(feature.id),
        stored=stored, lock=lock, loaded=True, easy_wiki_hidden=hidden,
    )


def _external_entry(plugin_id: str, state: plugins_external.PluginState | None,
                    row: dict[str, Any] | None) -> Entry:
    rt = plugins_external.runtime()
    manifest = state.manifest if state else None
    feature = registry.registry().features.get(plugin_id) if state and state.loaded else None
    name = (manifest.name if manifest else "") or (row["name"] if row else plugin_id)
    description = (manifest.description if manifest else "") or (row["description"] if row else "")
    if feature is not None and not state.legacy:
        name = i18n.t(feature.name, name)
        description = i18n.t(feature.description, description)
    stored = bool(row and row["enabled"])
    loaded = bool(state and state.loaded)
    entry = Entry(
        id=plugin_id, kind="external", name=name, description=description,
        version=(manifest.version if manifest else row["version"] if row else ""),
        author=(manifest.author if manifest else row["author"] if row else ""),
        enabled=loaded and registry.is_enabled(plugin_id), stored=stored, loaded=loaded,
        requires=manifest.requires if manifest else (), has_files=state is not None,
        legacy=bool(state and state.legacy), path=state.path if state else None,
    )
    if state is None:
        entry.lock = "plugin_manager.lock.missing"
    elif not rt.allowed:
        entry.lock = "plugin_manager.lock.external_off"
    elif _denied(plugin_id):
        entry.lock = "plugin_manager.lock.denylisted"
    if state is not None and state.error_key:
        entry.error_key, entry.error_values = state.error_key, state.error_values
    if stored and not loaded and state is not None and not state.error_key and rt.allowed and not _denied(plugin_id):
        entry.restart = "enable"
    elif loaded and not stored:
        entry.restart = "disable"
    return entry


def entries(*, include_hidden: bool = False) -> list[Entry]:
    """Every feature, plugin and retired row, features first."""
    reg = registry.registry()
    rt = plugins_external.runtime()
    rows = _rows()
    # A folder that could not be identified is listed separately, unless it replaced an installed plugin.
    external_ids = set(rt.plugins) | {pid for pid, row in rows.items() if not row["builtin"]}
    result: list[Entry] = []
    for feature in reg.ordered():
        if feature.id not in external_ids:
            result.append(_feature_entry(feature, rows))
    for plugin_id in sorted(external_ids - plugins_external.RETIRED_IDS, key=str.casefold):
        state = rt.plugins.get(plugin_id) or rt.invalid.get(plugin_id)
        result.append(_external_entry(plugin_id, state, rows.get(plugin_id)))
    for plugin_id, row in rows.items():
        if plugin_id in reg.features or (plugin_id in external_ids and plugin_id not in plugins_external.RETIRED_IDS):
            continue
        result.append(Entry(id=plugin_id, kind="retired", name=row["name"] or plugin_id,
                            description=row["description"], version=row["version"], author=row["author"],
                            stored=bool(row["enabled"]), lock="plugin_manager.lock.retired"))
    by_id = {entry.id: entry for entry in result}
    for entry in result:
        for dependency in entry.requires:
            if dependency in by_id:
                by_id[dependency].required_by.append(entry.id)
    if not include_hidden:
        result = [entry for entry in result if not entry.easy_wiki_hidden]
    return result


def get(plugin_id: str) -> Entry | None:
    return next((entry for entry in entries(include_hidden=True) if entry.id == plugin_id), None)


def names(ids: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    known = {entry.id: entry.name for entry in entries(include_hidden=True)}
    return [(plugin_id, known.get(plugin_id, plugin_id)) for plugin_id in ids]


def missing_requirements(entry: Entry) -> list[str]:
    known = {e.id: e for e in entries(include_hidden=True)}
    return [dep for dep in entry.requires if not (dep in known and known[dep].stored)]


def enabled_dependents(entry: Entry) -> list[str]:
    known = {e.id: e for e in entries(include_hidden=True)}
    return [dep for dep in entry.required_by if dep in known and known[dep].stored]


# ── Changes ──────────────────────────────────────────────────────────────────


def set_state(entry: Entry, enabled: bool) -> None:
    """Switch *entry*. Built-in features change at once; external plugins at the next start."""
    if entry.kind == "feature":
        registry.set_enabled(entry.id, enabled)
        return
    if entry.kind != "external":
        raise ValueError(f"{entry.id} cannot be switched")
    now = now_sql()
    if enabled:
        db.execute("UPDATE plugins SET enabled = 1, enabled_at = ? WHERE id = ? AND builtin = 0", (now, entry.id))
    else:
        db.execute("UPDATE plugins SET enabled = 0, disabled_at = ? WHERE id = ? AND builtin = 0", (now, entry.id))
    g.pop("_plugin_states", None)
    plugins_external.runtime().notify(entry.id, "enable" if enabled else "disable")


def remove_row(plugin_id: str) -> None:
    db.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))


# ── Password, backups, restart ───────────────────────────────────────────────


def password_ok(password: str) -> bool:
    """Check the password of the person at the keyboard (the admin, while impersonating)."""
    account = auth.real_user()
    return bool(account) and 0 < len(password) <= passwords.MAX_LENGTH and \
        passwords.verify_password(account["password"], password)


def backup_database(plugin_id: str) -> Path:
    """Copy the database aside before plugin code first runs or plugin data is dropped."""
    cfg = _cfg()
    root = Path(cfg.instance_dir) / SNAPSHOT_DIR
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = "".join(ch for ch in plugin_id if ch.isalnum() or ch in "_-")[:64] or "plugin"
    target = root / f"{stamp}-{safe_id}.db"
    counter = 1
    while target.exists():
        counter += 1
        target = root / f"{stamp}-{safe_id}-{counter}.db"
    current_app.extensions["bananawiki.database"].backup_to(target)
    ours = sorted((p for p in root.iterdir() if _SNAPSHOT_NAME.fullmatch(p.name) and not p.is_symlink()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in ours[SNAPSHOT_KEEP:]:
        stale.unlink(missing_ok=True)
    return target


def restart_mode() -> str:
    """``gunicorn`` when a SIGHUP to the master reloads the code, else ``preloaded`` or ``manual``."""
    if not request.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn/"):
        return "manual"
    if plugins_external.runtime().loaded_pid != os.getpid():
        return "preloaded"  # the app was imported by the master (preload_app): HUP keeps the old code
    return "gunicorn"


def request_restart() -> bool:
    """Ask the Gunicorn master to replace its workers; False when that cannot reload plugins.

    Shares the server page's safeguards (master detection and the one-a-minute
    cooldown) so both restart buttons behave the same.
    """
    from ..site_admin import server

    if restart_mode() != "gunicorn":
        return False
    master = server.gunicorn_master_pid()
    if not master or not server.claim_restart():
        return False
    server.restart(master)
    return True


# ── Sidebar order ────────────────────────────────────────────────────────────


def sidebar_apps() -> list[dict[str, Any]]:
    """Features with a sidebar entry, in the saved order."""
    saved = []
    for key in (settings.get("sidebar_apps_order") or "").split(","):
        key = LEGACY_APP_KEYS.get(key.strip(), key.strip())
        if key and key not in saved:
            saved.append(key)
    rank = {key: index for index, key in enumerate(saved)}
    apps = []
    for feature in registry.registry().ordered():
        items = [item for item in feature.nav if item.area == "apps"]
        if not items or (_cfg().easy_wiki and not feature.easy_wiki):
            continue
        apps.append({"id": feature.id, "label": i18n.t(items[0].label), "enabled": registry.is_enabled(feature.id),
                     "order": min(item.order for item in items)})
    apps.sort(key=lambda app: (rank.get(app["id"], 1000 + app["order"]), app["label"]))
    return apps


def save_sidebar_order(ids: list[str]) -> str:
    known = {app["id"] for app in sidebar_apps()}
    ordered: list[str] = []
    for plugin_id in ids:
        if plugin_id in known and plugin_id not in ordered:
            ordered.append(plugin_id)
    value = ",".join(ordered)[:MAX_ORDER_LENGTH]
    settings.update({"sidebar_apps_order": value})
    return value


def move(ids: list[str], plugin_id: str, direction: str) -> list[str]:
    """Move *plugin_id* one place up or down in *ids* (the no-script reorder buttons)."""
    ids = list(ids)
    if plugin_id not in ids:
        return ids
    index = ids.index(plugin_id)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(ids):
        ids[index], ids[target] = ids[target], ids[index]
    return ids
