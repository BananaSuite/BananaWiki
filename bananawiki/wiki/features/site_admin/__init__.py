"""Site-wide administration: general settings, appearance, languages, built-in
documentation, whole-site export and import, and the server page.

Pages extend ``admin/_layout.html`` (see ``features/admin/__init__.py``).
Security-relevant changes are written to the audit log (``features/audit``).
"""

from ... import auth
from ...registry import Feature, Job, NavItem
from . import (  # noqa: F401
    general,
    routes_appearance,
    routes_docs,
    routes_languages,
    routes_migration,
    routes_server,
)
from .blueprint import bp
from .migration import cleanup_stale
from .slots import public_notice


def _is_admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


FEATURE = Feature(
    id="site_admin",
    name="feature.site_admin.name",
    description="feature.site_admin.description",
    toggle="always",
    blueprints=[bp],
    nav=[
        NavItem("site_admin.nav.settings", "site_admin.general", icon="gear", area="admin", order=41,
                visible=_is_admin),
        NavItem("site_admin.nav.appearance", "site_admin.appearance", icon="palette", area="admin", order=42,
                visible=_is_admin),
        NavItem("site_admin.nav.languages", "site_admin.languages_page", icon="globe", area="admin", order=43,
                visible=_is_admin),
        NavItem("site_admin.nav.docs", "site_admin.docs_page", icon="book", area="admin", order=44,
                visible=_is_admin),
        NavItem("site_admin.nav.migration", "site_admin.migration_page", icon="archive", area="admin", order=96,
                visible=_is_admin),
        NavItem("site_admin.nav.server", "site_admin.server_page", icon="server", area="admin", order=97,
                visible=_is_admin),
    ],
    slots={"page.banners": public_notice},
    jobs=[Job("site_admin.cleanup_exports", 3600, cleanup_stale, initial_delay=300)],
    order=6,
)
