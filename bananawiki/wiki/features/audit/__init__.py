"""Audit log: security-relevant actions, kept for a configurable number of days.

Other features call :func:`record` (see :mod:`.service` for the contract).
"""

from ... import auth
from ...registry import Feature, Job, NavItem
from .handlers import EVENTS
from .routes import bp
from .service import prune, record

__all__ = ["FEATURE", "record"]


def _is_admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


FEATURE = Feature(
    id="audit",
    name="feature.audit.name",
    description="feature.audit.description",
    toggle="plugin",
    default_enabled=True,
    blueprints=[bp],
    nav=[NavItem("audit.nav", "audit.index", icon="list", area="admin", order=45, visible=_is_admin)],
    events=EVENTS,
    jobs=[Job("audit.prune", 86400, prune, initial_delay=600)],
    order=10,
)
