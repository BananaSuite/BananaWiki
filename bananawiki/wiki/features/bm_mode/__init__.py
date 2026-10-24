"""BM mode: the easy mode of a wiki (see :mod:`bananawiki.wiki.bm`).

* **Admin → BM mode** (``/admin/bm-mode``): switch BM mode, choose which
  features it shows and which simplifications apply.
* **Customise** (``account.display_sections``): BM mode for one's own account,
  where the wiki and the account allow it.
* **Admin → Users → an account** (``admin.user_detail``): that account's
  BM mode and whether it may switch it itself.
* **Admin dashboard** (``admin.dashboard``): a "getting started" checklist
  while the ``bm_mode.getting_started`` simplification applies.
"""

from ... import auth
from ...registry import Feature, NavItem, Simplification
from . import routes
from .routes import bp

FEATURE = Feature(
    id="bm_mode",
    name="feature.bm_mode.name",
    description="feature.bm_mode.description",
    toggle="always",
    blueprints=[bp],
    nav=[NavItem("bm_mode.nav", "bm_mode.settings", icon="sparkles", area="admin", order=43,
                 visible=lambda user: bool(user) and auth.is_admin(user))],
    slots={
        "admin.dashboard": routes.dashboard_card,
        "account.display_sections": routes.display_section,
        "admin.user_detail": routes.user_section,
    },
    simplifications=[
        Simplification("bm_mode.getting_started", "bm_mode.simplify.getting_started",
                       "bm_mode.simplify.getting_started_hint"),
    ],
    order=5,
)
