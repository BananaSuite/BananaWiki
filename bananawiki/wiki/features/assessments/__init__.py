"""Assessments: quizzes and polls attached to wiki pages.

Emits ``assessment.completed`` (``user_id``, ``page_id``, ``assessment_id``,
``attempt_id``, ``score``, ``max_score``) after every submitted attempt, and
offers :func:`service.total_points` for features that reward points.
"""

from ... import auth, settings
from ...registry import Feature, NavItem
from .routes import POINTS_SETTING, bp
from .slots import account_settings_section, page_below_content, page_header_actions

FEATURE = Feature(
    id="assessments",
    name="feature.assessments.name",
    description="feature.assessments.description",
    toggle="plugin",
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("assessments.nav.points", "assessments.points", icon="award", area="user",
                visible=lambda user: bool(user) and bool(settings.get(POINTS_SETTING))),
        NavItem("assessments.nav.admin", "assessments.admin", icon="check-square", area="admin",
                visible=lambda user: auth.is_admin(user) if user else False),
    ],
    slots={
        "page.header_actions": page_header_actions,
        "page.below_content": page_below_content,
        "account.settings_sections": account_settings_section,
    },
)
