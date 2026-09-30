"""Contributions: edits proposed by people who may read a page but not edit it.

The feature is switched by the ``contribution_approval_enabled`` setting (in
1.4 it also needed the page_governance plugin; migration 4 carries that
over, see :mod:`.schema`). It hooks into the pages feature through the
``page.edit_denied`` interceptor and the page slots.
"""

from ... import attention, auth
from ...registry import AttentionSource, Feature, Job, NavItem
from . import hooks, quota, service


def _admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


def _reviews(user) -> int:
    return service.review_count(user) if service.is_reviewer(user) else 0

from .routes import bp

FEATURE = Feature(
    id="contributions",
    name="feature.contributions.name",
    description="feature.contributions.description",
    toggle="setting",
    setting="contribution_approval_enabled",
    default_enabled=False,
    easy_wiki=False,
    blueprints=[bp],
    nav=[
        NavItem("contributions.nav.mine", "contributions.mine", icon="edit", area="user", order=50,
                visible=lambda user: bool(user) and auth.has_permission("contribution.propose", user)),
        NavItem("contributions.nav.review", "contributions.review_list", icon="check", area="user", order=51,
                visible=lambda user: service.is_reviewer(user) and not auth.is_admin(user),
                badge=attention.badge("contributions.reviews")),
        NavItem("contributions.nav.review", "contributions.review_list", icon="check", area="admin", order=62,
                visible=_admin, badge=lambda user: sum(attention.badge(source)(user) for source in (
                    "contributions.reviews", "contributions.quota_requests"))),
    ],
    attention=[
        AttentionSource("contributions.reviews", "attention.source.contributions.reviews",
                        "contributions.review_list", _reviews, oldest=service.oldest_for_reviewer,
                        audience="editor", order=20),
        AttentionSource("contributions.quota_requests", "attention.source.contributions.quota_requests",
                        "contributions.review_list", lambda user: quota.count_pending() if _admin(user) else 0,
                        oldest=lambda user: quota.oldest_pending(), order=21),
    ],
    jobs=[Job("contributions.expire", 3600, service.expire)],
    events={"user.role_changed": [service.withdraw_if_editing_allowed]},
    interceptors={"page.edit_denied": hooks.offer_proposal},
    slots={"page.header_actions": hooks.header_actions, "page.above_content": hooks.above_content},
    order=50,
)
