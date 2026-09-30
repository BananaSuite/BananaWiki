"""Deletion slowdown: a 48-hour grace period before deleted pages are gone.

Switched by the ``deletion_slowdown`` row in ``plugins`` (as in 1.4). It
takes over page deletion through the ``page.delete`` interceptor; the purge
runs as a job, so it stops while the feature is off (see :mod:`.service` for
what happens to pages that were already waiting).
"""

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import hooks, service
from .routes import bp


def _admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


FEATURE = Feature(
    id="deletion_slowdown",
    name="feature.deletion_slowdown.name",
    description="feature.deletion_slowdown.description",
    toggle="plugin",
    default_enabled=False,
    blueprints=[bp],
    nav=[NavItem("deletion_slowdown.nav", "deletion_slowdown.pending", icon="trash", area="admin", order=64,
                 visible=_admin, badge=attention.badge("deletion_slowdown.pending"))],
    attention=[AttentionSource("deletion_slowdown.pending", "attention.source.deletion_slowdown.pending",
                               "deletion_slowdown.pending", lambda user: service.count_pending() if _admin(user) else 0,
                               oldest=lambda user: service.oldest_pending(), order=50)],
    jobs=[Job("deletion_slowdown.purge", 600, service.purge_due)],
    interceptors={"page.delete": hooks.schedule_instead},
    slots={"page.above_content": hooks.pending_notice},
    order=60,
)
