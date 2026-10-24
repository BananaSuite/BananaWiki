"""BM mode: the easy mode of a wiki, adjustable item by item.

BM mode replaced the "easy wiki" switch (``BW_EASY_WIKI``). While it is on:

* **Features** that are not meant for a first wiki (``Feature.easy_wiki =
  False``: chats, quizzes, read aloud, the page builder, governance, custom
  pages…) are hidden and off, until an administrator shows them in BM mode.
  Any feature can be hidden in BM mode too. Nothing is deleted: a hidden
  feature keeps its data and its own switch, and comes back as it was.
* **Simplifications** declared by features (``Feature.simplifications``)
  hide the less used controls of the editor, the page actions, the
  administration menu, kanban boards, canvases and so on. Each one is on by
  default and can be turned off.

BM mode is on for the whole wiki (everyone is in it) or off. While it is off,
each account can be in BM mode on its own (``users.bm_mode``): that person
gets the same simplifications, and the features BM mode hides leave their
menus and answer 404 for them (:func:`hidden_for_viewer`), while the features
keep working for the wiki (jobs, the REST API, checks on everyone's actions).
Administrators switch any account, or all of them; people switch their own
when the wiki allows it (``site_settings.bm_self_service``) unless their
account says otherwise (``users.bm_self``: ``allow`` or ``deny``). New
accounts start in BM mode when ``site_settings.bm_new_accounts`` says so.

Where BM mode comes from: ``site_settings.bm_mode`` once an administrator
chose, else ``BW_EASY_WIKI`` (the hosting platform's "easy mode" switch). The
operator's value is remembered with the choice (``bm_mode_env``): when the
operator changes it later (the platform switch flipped), the operator's value
applies again until an administrator chooses anew. The choices per item are
JSON in ``site_settings.bm_choices``: ``{"feature:<id>": bool,
"simplify:<id>": bool}``.

Simplifications only change what is shown. Pages they hide stay reachable
and every permission check stays where it is.
"""

from __future__ import annotations

import json
import re
from typing import Any

from flask import current_app, g, has_app_context, has_request_context, request
from markupsafe import Markup

from . import settings

# Whether one account may switch its BM mode itself: the wiki's rule, always, never.
SELF_RULES = ("", "allow", "deny")
_ID = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")


def _cfg() -> Any:
    return current_app.config["BW"]


def _operator_default() -> int:
    return 1 if _cfg().easy_wiki else 0


def active() -> bool:
    """Whether the wiki is in BM mode."""
    stored = settings.get("bm_mode")
    if stored is None or settings.get("bm_mode_env") != _operator_default():
        return bool(_operator_default())
    return bool(stored)


def choices() -> dict[str, bool]:
    """The administrators' choices per item (cached per request)."""
    if has_request_context():
        cached = g.get("_bm_choices")
        if cached is not None:
            return cached
    try:
        raw = json.loads(settings.get("bm_choices") or "{}")
    except (TypeError, ValueError):
        raw = {}
    parsed = {str(key): bool(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
    if has_request_context():
        g._bm_choices = parsed
    return parsed


def third_party(feature_id: str) -> bool:
    """Whether *feature_id* is a loaded third-party plugin.

    BM mode leaves those as **Admin → Plugins** sets them: showing one again
    would run plugin code, which only owners and superusers may switch on.
    """
    from .plugins_external import EXTENSION

    if not has_app_context() or EXTENSION not in current_app.extensions:
        return False
    return feature_id in current_app.extensions[EXTENSION].plugins


def feature_shown_choice(feature: Any) -> bool:
    """Whether BM mode shows *feature* when it is on (the administrators' choice, else the feature's default)."""
    if feature.toggle == "always" or third_party(feature.id):
        return True
    return choices().get(f"feature:{feature.id}", bool(feature.easy_wiki))


def feature_shown(feature: Any) -> bool:
    """Whether *feature* exists right now as far as BM mode is concerned (always outside BM mode)."""
    if feature.toggle == "always" or not active():
        return True
    return feature_shown_choice(feature)


def _api_request() -> bool:
    return has_request_context() and request.path.startswith("/api/")


def personal(user: dict[str, Any] | None = None) -> bool:
    """Whether *user* (by default the person looking at the page) has BM mode on for their own account."""
    if user is None:
        if not has_request_context():
            return False
        from . import auth

        user = auth.current_user()
    try:
        return bool(user and int(user.get("bm_mode") or 0))
    except (TypeError, ValueError):
        return False


def applies(user: dict[str, Any] | None = None) -> bool:
    """Whether BM mode applies to *user* (default: the person looking): the wiki's, or their own."""
    return active() or personal(user)


def hidden_for_viewer(feature: Any) -> bool:
    """Whether *feature* is hidden from the person looking at this page by their own BM mode.

    Only their menus, pages and page fragments change (see
    :func:`bananawiki.wiki.registry.shown`): the feature keeps working for the
    wiki, so its background jobs, its REST API and the checks it makes on
    everyone's actions (page protection, for example) still run.
    """
    if feature is None or active() or feature.toggle == "always" or _api_request() or not personal():
        return False
    return not feature_shown_choice(feature)


# Who switches an account's BM mode


def self_service() -> bool:
    """The wiki's rule: everyone may switch BM mode for their own account (unless their account says otherwise)."""
    return bool(settings.get("bm_self_service", 1))


def new_accounts() -> bool:
    """Whether new accounts start in BM mode."""
    return bool(settings.get("bm_new_accounts", 0))


def may_switch_self(user: dict[str, Any] | None) -> bool:
    """Whether *user* may switch BM mode for their own account (never while the whole wiki is in it)."""
    if not user or active():
        return False
    rule = str(user.get("bm_self") or "")
    return rule == "allow" or (rule not in SELF_RULES[1:] and self_service())


def set_personal(user_id: str, on: bool) -> bool:
    """Put one account in BM mode or take it out; returns whether that changed anything."""
    from .db import db

    changed = db.execute("UPDATE users SET bm_mode = ? WHERE id = ? AND bm_mode != ?",
                         (1 if on else 0, user_id, 1 if on else 0)).rowcount
    forget()
    return bool(changed)


def set_rule(user_id: str, rule: str) -> bool:
    """Whether one account may switch on its own: ``""`` (the wiki's rule), ``allow`` or ``deny``."""
    from .db import db

    if rule not in SELF_RULES:
        raise ValueError(rule)
    return bool(db.execute("UPDATE users SET bm_self = ? WHERE id = ? AND bm_self != ?",
                           (rule, user_id, rule)).rowcount)


def set_everyone(on: bool, *, condition: str = "1", params: list[Any] | tuple[Any, ...] = ()) -> int:
    """Put every account matching the SQL *condition* in BM mode or take it out; returns how many changed."""
    from .db import db

    value = 1 if on else 0
    changed = db.execute(f"UPDATE users SET bm_mode = ? WHERE bm_mode != ? AND {condition}",  # noqa: S608
                         (value, value, *params)).rowcount
    forget()
    return changed


def accounts_in_bm_mode() -> int:
    from .db import db

    return int(db.scalar("SELECT COUNT(*) FROM users WHERE bm_mode = 1", default=0) or 0)


def simplifications() -> list[Any]:
    """Every simplification of the features that exist now, in feature order."""
    from .registry import is_enabled, registry

    return [item for feature in registry().ordered() if is_enabled(feature.id) for item in feature.simplifications]


def chosen(item: Any) -> bool:
    """Whether simplification *item* is switched on (it applies only where :func:`applies`)."""
    return choices().get(f"simplify:{item.id}", bool(item.default))


def applied() -> list[str]:
    """Ids of the simplifications applying to this request."""
    if has_request_context():
        cached = g.get("_bm_applied")
        if cached is not None:
            return cached
    result = [item.id for item in simplifications() if chosen(item)] if applies() else []
    if has_request_context():
        g._bm_applied = result
    return result


def simplified(simplification_id: str) -> bool:
    """Whether simplification *simplification_id* applies to this request (for templates and views)."""
    return simplification_id in applied()


def css() -> Markup:
    """Rules hiding what the applied simplifications hide (``data-bm-hide`` markers)."""
    rules = [f'[data-bm-hide~="{item}"]{{display:none!important}}' for item in applied() if _ID.match(item)]
    return Markup("".join(rules))


# Changing


def save(*, mode: bool, features: dict[str, bool], simplifications_on: dict[str, bool],
         self_service_on: bool | None = None, new_accounts_on: bool | None = None) -> dict[str, str]:
    """Store BM mode, the choices per item (unknown items are dropped) and, when given, the rules for accounts.

    Hiding a feature counts as switching it off: a feature the person at the
    keyboard may not switch off (the audit log, for plain administrators)
    stays shown. Returns those features with the reason (a translation key).
    """
    from .registry import registry, switch_refusal

    known_features = {feature.id for feature in registry().ordered()
                      if feature.toggle != "always" and not third_party(feature.id)}
    by_id = registry().features
    kept: dict[str, str] = {}
    for key, value in features.items():
        if key in known_features and not value and feature_shown_choice(by_id[key]):
            refusal = switch_refusal(key, False)
            if refusal is not None:
                kept[key] = refusal
    features = {**features, **{key: True for key in kept}}
    known_items = {item.id for feature in registry().ordered() for item in feature.simplifications}
    stored = {key: value for key, value in choices().items()
              if (key.startswith("feature:") and key[8:] not in known_features)
              or (key.startswith("simplify:") and key[9:] not in known_items)}  # plugins that are not loaded now
    stored.update({f"feature:{key}": bool(value) for key, value in features.items() if key in known_features})
    stored.update({f"simplify:{key}": bool(value) for key, value in simplifications_on.items()
                   if key in known_items})
    values: dict[str, Any] = {"bm_mode": 1 if mode else 0, "bm_mode_env": _operator_default(),
                              "bm_choices": json.dumps(stored, sort_keys=True)}
    if self_service_on is not None:
        values["bm_self_service"] = 1 if self_service_on else 0
    if new_accounts_on is not None:
        values["bm_new_accounts"] = 1 if new_accounts_on else 0
    settings.update(values)
    forget()
    return kept


def show_feature(feature_id: str, shown: bool = True) -> None:
    """Show (or hide) one feature in BM mode, keeping every other choice."""
    current = choices()
    settings.update({"bm_choices": json.dumps({**current, f"feature:{feature_id}": bool(shown)}, sort_keys=True)})
    forget()


def forget() -> None:
    """Drop what this request cached (after a change)."""
    if has_request_context():
        for key in ("_bm_choices", "_bm_applied", "_plugin_states"):
            g.pop(key, None)
