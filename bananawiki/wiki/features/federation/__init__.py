"""Federation: pair with other BananaWiki installations and share individual pages read only.

Off unless the operator sets ``BW_FEDERATION_ENABLED=1`` (there is no
administrator switch, as in 1.4). See FEDERATION.md.
"""

from ...auth import is_admin
from ...registry import Feature, Job, NavItem
from . import sync
from .routes import bp, nav_visible

FEATURE = Feature(
    id="federation",
    name="feature.federation.name",
    description="feature.federation.description",
    toggle="always",
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("federation.nav.received", "federation.index", icon="globe", area="apps", order=90,
                visible=nav_visible),
        NavItem("federation.nav.admin", "federation.admin", icon="globe", area="admin", order=90,
                visible=lambda user: bool(user) and is_admin(user) and sync.active()),
    ],
    jobs=[Job("federation.poll", 30, sync.poll)],
)
