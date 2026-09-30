"""Page governance: page protection and page reservations (check-outs).

The ``page_governance`` row in ``plugins`` switches the feature (as in 1.4);
``page_protection_enabled`` and ``page_reservations_enabled`` switch its two
parts. Integration with the pages feature goes through the
``page.edit_blocked``, ``page.delete`` and ``page.saved`` interceptors and
the page and editor slots.
"""

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import admin, hooks, reservations  # noqa: F401 - admin registers its views
from .quota import count_pending, oldest_pending
from .routes import bp


def _editor(user) -> bool:
    return bool(user) and auth.has_role("editor", user) and reservations.active()


def _admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


FEATURE = Feature(
    id="page_governance",
    name="feature.page_governance.name",
    description="feature.page_governance.description",
    toggle="plugin",
    default_enabled=False,
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("page_governance.nav.directory", "page_governance.directory", icon="lock", area="apps",
                order=60, visible=_editor),
        NavItem("page_governance.nav.quota", "page_governance.my_quota", icon="lock", area="user",
                order=60, visible=lambda user: _editor(user) and not auth.is_admin(user)),
        NavItem("page_governance.nav.checkouts", "page_governance.checkouts", icon="lock", area="admin",
                order=60, visible=_admin, badge=attention.badge("page_governance.quota_requests")),
        NavItem("page_governance.nav.governance", "page_governance.governance", icon="shield", area="admin",
                order=61, visible=_admin),
    ],
    attention=[
        AttentionSource("page_governance.quota_requests", "attention.source.page_governance.quota_requests",
                        "page_governance.checkouts", lambda user: count_pending() if _admin(user) else 0,
                        oldest=lambda user: oldest_pending(), order=30),
    ],
    jobs=[Job("page_governance.cleanup", 3600, reservations.cleanup)],
    interceptors={
        "page.edit_blocked": hooks.edit_blocked,
        "page.delete": hooks.refuse_delete,
        "page.saved": hooks.reserve_after_save,
    },
    slots={
        "page.header_actions": hooks.header_actions,
        "page.above_content": hooks.above_content,
        "editor.below_form": hooks.editor_below_form,
    },
    order=40,
)
