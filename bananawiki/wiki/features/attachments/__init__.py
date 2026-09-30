"""Page attachments: files attached to wiki pages (the 1.4 "attachments" plugin).

Adds the attachment list under every page (``page.below_content``) and to
the editor (``editor.below_form``). Deleting a page removes its files (pages
service) and this feature drops any 1.4 ``file_blobs`` copies left behind.
"""

from ...registry import Feature, Job
from . import service
from .routes import bp
from .slots import editor_panel, page_panel

FEATURE = Feature(
    id="attachments",
    name="feature.attachments.name",
    description="feature.attachments.description",
    toggle="plugin",
    blueprints=[bp],
    jobs=[Job("attachments.cleanup", 24 * 3600, service.cleanup, initial_delay=900)],
    events={"page.deleted": [service.on_page_deleted]},
    slots={"page.below_content": page_panel, "editor.below_form": editor_panel},
)
