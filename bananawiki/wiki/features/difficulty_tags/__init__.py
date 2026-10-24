"""Difficulty tags: a difficulty level or custom label per page (the 1.4 "difficulty_tags" plugin)."""

from ...registry import Feature, Simplification
from .routes import bp, on_page_saved
from .slots import HIDE_TAGS, badge, editor_fields, header_action

FEATURE = Feature(
    id="difficulty_tags",
    name="feature.difficulty_tags.name",
    description="feature.difficulty_tags.description",
    toggle="plugin",
    blueprints=[bp],
    interceptors={"page.saved": on_page_saved},
    slots={"page.above_content": badge, "page.header_actions": header_action, "editor.below_form": editor_fields},
    simplifications=[
        Simplification(HIDE_TAGS, "difficulty_tags.bm.hide_tags", "difficulty_tags.bm.hide_tags_hint"),
    ],
)
