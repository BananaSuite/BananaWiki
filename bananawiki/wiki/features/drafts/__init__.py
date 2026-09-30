"""Drafts: autosaved, per-user drafts of page edits (the 1.4 "drafts" plugin).

The editor slot adds autosave, restore/discard and the list of other
editors' open drafts; saving the page (``page.saved``) clears the drafts and
credits their authors. Drafts expire after ``draft_expiration_hours``.
"""

from ... import auth
from ...registry import Feature, Job, NavItem
from . import service
from .routes import bp
from .slots import editor_panel, settings_section


def _has_drafts_permission(user) -> bool:
    return bool(user) and auth.has_permission("draft.view_own", user)


FEATURE = Feature(
    id="drafts",
    name="feature.drafts.name",
    description="feature.drafts.description",
    toggle="plugin",
    blueprints=[bp],
    nav=[NavItem("drafts.nav", "drafts.mine", icon="file-text", area="user", order=40,
                 visible=_has_drafts_permission)],
    jobs=[Job("drafts.expire", 3600, service.expire, initial_delay=300)],
    interceptors={"page.saved": service.on_page_saved},
    slots={"editor.below_form": editor_panel, "account.settings_sections": settings_section},
)
