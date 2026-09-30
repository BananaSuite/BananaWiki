"""Administration area: dashboard, accounts, custom roles, invite codes and sessions.

The admin layout
----------------
Every administration page extends ``admin/_layout.html``. It renders the
admin side menu, built from the ``NavItem(area="admin")`` entries of every
enabled feature, next to the page body. A feature adds an admin page by

1. declaring a nav entry in its ``Feature``::

       nav=[NavItem("myfeature.admin.nav", "myfeature.admin_page", icon="gear",
                    area="admin", order=60)]

   (``order`` places it in the menu: the dashboard is 0, users 10, roles 20,
   invite codes 30, sessions 40; use 50 and up for other pages), and

2. writing a template that extends the layout and fills its blocks::

       {% extends "admin/_layout.html" %}
       {% block title %}{{ t('myfeature.admin.title') }}{% endblock %}
       {% block admin_content %}
         <header class="page-header"><h1>{{ t('myfeature.admin.title') }}</h1></header>
         ...
       {% endblock %}

   ``admin_head`` adds tags to ``<head>`` and ``admin_scripts`` adds scripts
   at the end of the page. The view itself must still check authorisation
   (``@auth.admin_required``); the layout only draws the menu.

The dashboard renders the ``admin.dashboard`` slot, so features can add
cards to it without owning a page.
"""

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import analytics, jobs, routes  # noqa: F401 - registers the views
from .blueprint import bp
from .service import pending_count, pending_oldest


def _is_admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


def _invite_manager(user) -> bool:
    """Non-administrators that were granted invite-code permissions."""
    return bool(user) and not auth.is_admin(user) and (
        auth.has_permission("invite.view", user) or auth.has_permission("invite.generate", user)
    )


def _pending_signups(user) -> int:
    return pending_count() if _is_admin(user) else 0


FEATURE = Feature(
    id="admin",
    name="feature.admin.name",
    description="feature.admin.description",
    toggle="always",
    blueprints=[bp],
    nav=[
        NavItem("admin.nav.dashboard", "admin.dashboard", icon="gauge", area="admin", order=0, visible=_is_admin),
        NavItem("admin.nav.users", "admin.users", icon="users", area="admin", order=10, visible=_is_admin,
                badge=attention.badge("admin.signups")),
        NavItem("admin.nav.roles", "admin.roles", icon="shield", area="admin", order=20, visible=_is_admin),
        NavItem("admin.nav.codes", "admin.codes", icon="ticket", area="admin", order=30, visible=_is_admin),
        NavItem("admin.nav.sessions", "admin.sessions", icon="key", area="admin", order=40, visible=_is_admin),
        NavItem("admin.nav.codes", "admin.codes", icon="ticket", area="user", order=80, visible=_invite_manager),
    ],
    attention=[
        AttentionSource("admin.signups", "attention.source.admin.signups", "admin.users", _pending_signups,
                        endpoint_args={"approval": "pending"}, oldest=lambda user: pending_oldest(), order=10),
    ],
    jobs=[
        Job("admin.account_cleanup", 900, jobs.account_cleanup),
        Job("admin.auto_logout", 300, jobs.auto_logout),
    ],
    init_app=analytics.install,
    order=5,
)
