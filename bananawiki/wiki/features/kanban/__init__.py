"""Kanban boards: columns, tickets, comments, attachments, history, sharing and embeds."""

from ...registry import Feature, Job, NavItem
from . import api  # noqa: F401 - registers the JSON routes on the blueprint
from .embed import page_scripts
from .events import prune
from .extras import sweep_orphan_files
from .service import forget_user
from .views import bp, nav_visible

FEATURE = Feature(
    id="kanban",
    name="feature.kanban.name",
    description="feature.kanban.description",
    toggle="plugin",
    default_enabled=True,
    easy_wiki=False,
    blueprints=[bp],
    nav=[NavItem("kanban.nav", "kanban.index", icon="columns", area="apps", order=40, visible=nav_visible)],
    jobs=[
        Job("kanban.prune_events", 3600, prune),
        Job("kanban.sweep_files", 86400, sweep_orphan_files, initial_delay=600),
    ],
    events={"user.deleted": [forget_user]},
    slots={"page.scripts": page_scripts},
)
