"""Needs your attention: waiting requests, decision notices and email notifications.

The queues themselves are declared by the features that own them
(``Feature.attention``, see :mod:`bananawiki.wiki.attention`). This feature
shows them - the ``/attention`` page, the account menu (red dot and section),
the admin dashboard card and a banner on the first page after signing in -
and runs the ``attention.notify`` job that emails administrators and
reviewers (immediately, as a digest, or once a day) and tells people when
their own request was decided.

Events handled: ``attention.created``, ``attention.decided``,
``user.created`` (a sign-up waiting for approval) and ``user.login``.
"""

from __future__ import annotations

from ... import auth
from ...registry import Feature, Job, NavItem
from . import mailer, service, slots
from .routes import bp

FEATURE = Feature(
    id="attention",
    name="feature.attention.name",
    description="feature.attention.description",
    toggle="always",
    blueprints=[bp],
    nav=[NavItem("attention.admin.nav", "attention.admin_settings", icon="mail", area="admin", order=45,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    jobs=[Job("attention.notify", 60, mailer.run, initial_delay=60)],
    events={
        "attention.created": [service.on_created],
        "attention.decided": [service.on_decided],
        "user.created": [service.on_user_created],
        "user.login": [slots.on_login],
    },
    slots={
        "page.banners": slots.banners,
        "account.settings_sections": slots.settings_section,
        "auth.account_status": slots.account_status,
    },
    order=4,
)
