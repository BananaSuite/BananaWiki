"""Announcements: site-wide banners managed by administrators."""

from ... import auth
from ...registry import Feature, Job, NavItem
from . import service
from .routes import bp
from .slots import render_bar

FEATURE = Feature(
    id="announcements",
    name="feature.announcements.name",
    description="feature.announcements.description",
    toggle="plugin",
    blueprints=[bp],
    nav=[NavItem("announcements.nav.admin", "announcements.admin", icon="megaphone", area="admin",
                 visible=lambda user: auth.is_admin(user) if user else False)],
    jobs=[Job("announcements.prune", 86400, service.prune_expired)],
    slots={"page.banners": render_bar},
)
