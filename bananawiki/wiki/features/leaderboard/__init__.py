"""Contributor leaderboard (site setting ``contributor_leaderboard_enabled``)."""

from __future__ import annotations

from ...registry import Feature, Job, NavItem
from . import service
from .routes import bp

FEATURE = Feature(
    id="leaderboard",
    name="feature.leaderboard.name",
    description="feature.leaderboard.description",
    toggle="setting",
    setting="contributor_leaderboard_enabled",
    default_enabled=False,
    blueprints=[bp],
    nav=[NavItem("leaderboard.nav", "leaderboard.index", icon="trophy", area="apps", order=50)],
    jobs=[Job("leaderboard.refresh", 300, service.refresh_all, initial_delay=90)],
    order=40,
)
