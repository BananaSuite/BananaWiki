"""The audit log's watch over feature switches and the plugin manager.

Built-in features are switched with :func:`registry.set_enabled`, whichever
page does it (the plugin manager, the onboarding wizard). Its interceptors

* refuse to switch the audit log off for anyone but owners and superusers
  (``feature.switch``): a plain administrator must not be able to stop the
  record of their own actions;
* record every switch (``plugin.enabled``, ``plugin.disabled``;
  ``feature.switched``). Switching the audit log off is recorded as its last
  entry before it stops.

The plugin manager also installs, removes, updates and switches third-party
plugins without the registry. Hooks around its views (blueprint
``plugin_manager``) refuse its "switch off" button for the audit log with a
message and compare the state of every plugin before and after each of its
POST requests to record the rest (``plugin.installed``, ``plugin.removed``,
``plugin.updated`` and the switches of third-party plugins).
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, g, has_request_context, redirect, request, url_for

from ... import auth, registry, settings
from ...db import db
from .service import FEATURE_ID, record

PLUGIN_MANAGER = "plugin_manager"
OWNER_ONLY = "audit.disable.owner_only"
_SNAPSHOT = "_audit_plugin_states"
_RECORDED = "_audit_recorded_switches"

State = tuple[bool, str | None]


def _plain_administrator_at_the_keyboard() -> bool:
    """A signed-in account below owners and superusers is making this request (also while impersonating)."""
    from ..admin import service as admin_service  # the admin feature records through this one

    if not has_request_context():
        return False  # the server's own commands
    person = auth.real_user()
    return person is not None and not admin_service.is_top(person)


def refuse_switch(feature_id: str, enabled: bool, **_: Any) -> str | None:
    """``feature.switch`` interceptor: only owners and superusers switch the audit log off."""
    if feature_id != FEATURE_ID or enabled or not _plain_administrator_at_the_keyboard():
        return None
    return OWNER_ONLY


def record_switch(feature_id: str, enabled: bool, **_: Any) -> None:
    """``feature.switched`` interceptor (run while the audit log is off too): record the switch."""
    # The entry saying the audit log was switched off is written although it now is.
    record("plugin.enabled" if enabled else "plugin.disabled", target_type="plugin", target_id=feature_id,
           force=feature_id == FEATURE_ID)
    if has_request_context():
        g.setdefault(_RECORDED, set()).add(feature_id)


def _states() -> dict[str, State]:
    """Whether each feature and plugin is enabled (and an external plugin's version), read afresh."""
    g.pop("_plugin_states", None)
    settings.invalidate()
    states: dict[str, State] = {feature_id: (registry.is_enabled(feature_id), None)
                                for feature_id in registry.registry().features}
    for row in db.all("SELECT id, enabled, version FROM plugins WHERE builtin = 0"):
        states[row["id"]] = (bool(row["enabled"]), row["version"])
    return states


def _refuse_audit_switch_off() -> Any:
    if request.endpoint != f"{PLUGIN_MANAGER}.disable" or (request.view_args or {}).get("plugin_id") != FEATURE_ID:
        return None
    person = auth.real_user()
    if person is None or not auth.is_admin(person) or not _plain_administrator_at_the_keyboard():
        return None  # non-administrators get the plugin manager's own refusal
    record("audit.disable_refused", target_type="plugin", target_id=FEATURE_ID)
    auth.flash_t(OWNER_ONLY, "error")
    return redirect(url_for(f"{PLUGIN_MANAGER}.index"))


def before_plugin_manager() -> Any:
    if request.method != "POST":
        return None
    refused = _refuse_audit_switch_off()
    if refused is not None:
        return refused
    setattr(g, _SNAPSHOT, _states())
    return None


def _action(old: State | None, new: State | None) -> str:
    if old is None:
        return "plugin.installed"
    if new is None:
        return "plugin.removed"
    if old[0] != new[0]:
        return "plugin.enabled" if new[0] else "plugin.disabled"
    return "plugin.updated"


def after_plugin_manager(response: Response) -> Response:
    before = g.pop(_SNAPSHOT, None)
    if before is None:
        return response
    after = _states()
    recorded = g.get(_RECORDED, set())
    for plugin_id in sorted(set(before) | set(after)):
        old, new = before.get(plugin_id), after.get(plugin_id)
        if old == new or plugin_id in recorded:
            continue
        details = {"version": (new or old)[1]} if (new or old)[1] else {}  # type: ignore[index]
        record(_action(old, new), target_type="plugin", target_id=plugin_id, details=details,
               force=plugin_id == FEATURE_ID)
    return response


def install(app: Flask) -> None:
    """Run the hooks around the plugin manager's views (after the request pipeline signed the user in)."""
    app.before_request_funcs.setdefault(PLUGIN_MANAGER, []).append(before_plugin_manager)
    app.after_request_funcs.setdefault(PLUGIN_MANAGER, []).append(after_plugin_manager)
