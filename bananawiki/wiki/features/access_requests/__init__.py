"""Requests for edit access: members who can read a page but not edit it ask, administrators decide.

Off until an administrator ticks "Let people ask for edit access" on Admin →
People → Access requests (``site_settings.access_requests_enabled``). See
:mod:`.service` for what a request asks for and what approving it gives.
The pages feature shows the request button through the
``page.edit_unavailable`` slot, where the Edit button would be.
"""

from ... import attention, auth
from ...registry import AttentionSource, Feature, NavItem
from . import service
from .routes import bp


def _admin(user) -> bool:
    return bool(user) and auth.is_admin(user)


def _ask(page) -> str:
    from flask import render_template

    user = auth.current_user()
    offer = service.offer(page, user)
    if offer is None:
        return ""
    return render_template("access_requests/_ask.html", page=page, offer=offer, max_message=service.MAX_MESSAGE)


FEATURE = Feature(
    id="access_requests",
    name="feature.access_requests.name",
    description="feature.access_requests.description",
    toggle="always",
    blueprints=[bp],
    nav=[
        NavItem("access_requests.nav", "access_requests.admin", icon="lock", area="admin", order=15,
                visible=_admin, badge=attention.badge(service.SOURCE), bm_hide="admin.short_menu"),
    ],
    attention=[
        AttentionSource(service.SOURCE, "attention.source.access_requests.pending", "access_requests.admin",
                        lambda user: service.count_pending() if _admin(user) else 0,
                        oldest=lambda user: service.oldest_pending(), order=12),
    ],
    slots={"page.edit_unavailable": _ask},
    order=55,
)
