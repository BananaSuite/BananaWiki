"""REST API (``/api/v1``) with bearer tokens, audit log, userbot mode and their management pages.

The feature is the 1.4 ``api_service`` plugin (row in ``plugins``); the site
setting ``api_service_enabled`` is the administrator's on/off switch for the
API itself (503 while off). See API.md.
"""

from ... import attention
from ...auth import is_admin
from ...registry import AttentionSource, Feature, Job, NavItem
from . import audit, idempotency, tokens, webhooks
from .api import bp as api_bp
from .web import account_settings_section
from .web import bp as web_bp


def _revoke_after_password_change(user_id: str) -> None:
    """A token issued while the old password was known must stop working with it."""
    tokens.revoke_all_for_user(user_id)


def _revoke_for_user(user: dict, **_payload) -> None:
    """Administrator password reset or suspension: the account's tokens stop working for good."""
    tokens.revoke_all_for_user(user["id"])


def _prune() -> None:
    audit.prune()
    idempotency.prune()
    webhooks.prune()


def _events() -> dict:
    events: dict = {
        "user.password_changed": [_revoke_after_password_change],
        "user.password_reset": [_revoke_for_user],
        "user.suspended": [_revoke_for_user],
    }
    for event, handlers in webhooks.event_handlers().items():
        events.setdefault(event, []).extend(handlers)
    return events


FEATURE = Feature(
    id="api_service",
    name="feature.api_service.name",
    description="feature.api_service.description",
    toggle="plugin",
    default_enabled=False,
    blueprints=[api_bp, web_bp],
    nav=[NavItem("api_service.nav.admin", "api_service.admin", icon="plug", area="admin", order=80,
                 visible=lambda user: bool(user) and is_admin(user),
                 badge=attention.badge(webhooks.ATTENTION_SOURCE))],
    jobs=[
        Job("api_service.prune", 3600, _prune),
        Job("api_service.webhooks", 30, webhooks.deliver_due, initial_delay=30),
    ],
    events=_events(),
    attention=[AttentionSource(webhooks.ATTENTION_SOURCE, "api_service.attention.webhooks_disabled", "api_service.admin",
                               webhooks.disabled_count, endpoint_args={"_anchor": "webhooks"},
                               oldest=webhooks.oldest_disabled, order=95)],
    slots={"account.settings_sections": account_settings_section},
)
