"""Administration views, one module per area; all attach to :data:`..blueprint.bp`."""

from . import dashboard, invites, roles, sessions, users  # noqa: F401
