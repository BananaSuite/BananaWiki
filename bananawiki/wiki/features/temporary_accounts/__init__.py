"""Temporary accounts and pages: scheduled page deletion, timed hide/show,
account deletion and role reverts.

Switched by the ``temporary_accounts`` row in ``plugins`` (as in 1.4). The
expiry job only runs while the feature is on; switching it off pauses every
schedule without deleting it.
"""

from ... import auth
from ...registry import Feature, Job, NavItem
from . import hooks, service
from .routes import bp

FEATURE = Feature(
    id="temporary_accounts",
    name="feature.temporary_accounts.name",
    description="feature.temporary_accounts.description",
    toggle="plugin",
    default_enabled=False,
    blueprints=[bp],
    nav=[NavItem("temporary_accounts.nav", "temporary_accounts.overview", icon="clock", area="admin", order=65,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    jobs=[Job("temporary_accounts.expire", 300, service.run_expiry)],
    events={"user.role_changed": [service.on_role_changed]},
    interceptors={"page.delete": hooks.refuse_scheduled_delete},
    slots={
        "page.above_content": hooks.page_countdown,
        "account.settings_sections": hooks.account_settings_section,
        "profile.sections": hooks.profile_section,
    },
    order=45,
)
