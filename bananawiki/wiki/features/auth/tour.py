"""The guided tour: an illustrative walkthrough of the wiki for each role.

1.4 ran the tour on real pages and, for "role previews", swapped the
visitor's role for the preview role while those pages rendered, which let
any user read administration pages and restricted categories (audit H1).
Here every step is a static illustration rendered by the tour itself:
choosing another role changes which explanations are shown, never what the
account can see or do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ... import settings
from ...i18n import t
from ...permissions import ROLE_RANK
from ...registry import is_enabled, registry

ROLES = ("user", "editor", "admin")
DEFAULT_SWITCHING_ROLES = "user,editor,admin"


@dataclass(frozen=True)
class Step:
    id: str
    min_role: str
    figure: str


STEPS = (
    Step("home", "user", "home"),
    Step("search", "user", "search"),
    Step("personal_settings", "user", "settings"),
    Step("apps", "user", "apps"),
    Step("create_page", "editor", "editor"),
    Step("history", "editor", "history"),
    Step("admin_settings", "admin", "admin_settings"),
    Step("admin_users", "admin", "admin_users"),
    Step("admin_features", "admin", "admin_features"),
)


def real_role(user: dict[str, Any]) -> str:
    role = user.get("role") or "user"
    if role in ("admin", "owner"):
        return "admin"
    return role if role in ROLES else "user"


def role_options(user: dict[str, Any]) -> list[str]:
    """Perspectives *user* may choose (``intro_role_switching_*`` settings)."""
    own = real_role(user)
    if not settings.get("intro_role_switching_enabled", 1):
        return [own]
    allowed = {part.strip().lower() for part in
               str(settings.get("intro_role_switching_roles") or DEFAULT_SWITCHING_ROLES).split(",")}
    return list(ROLES) if own in allowed else [own]


def normalize_role(value: str | None, user: dict[str, Any]) -> str:
    options = role_options(user)
    return value if value in options else real_role(user)


def enabled_apps() -> list[str]:
    """Names of the enabled features that add an app to the sidebar (labels only, no data)."""
    names = []
    for feature in registry().ordered():
        if any(item.area == "apps" for item in feature.nav) and is_enabled(feature.id):
            names.append(t(feature.name))
    return names


def steps_for(role: str) -> list[Step]:
    rank = ROLE_RANK.get(role, 0)
    chosen = [step for step in STEPS if ROLE_RANK[step.min_role] <= rank]
    if not enabled_apps():
        chosen = [step for step in chosen if step.id != "apps"]
    return chosen
