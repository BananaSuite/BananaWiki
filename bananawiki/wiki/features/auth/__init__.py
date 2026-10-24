"""Sign-in: setup, password, portal and two-step sign-in, sign-up, account status, onboarding and the tour."""

from ...registry import Feature, Job
from . import (  # noqa: F401 - the route modules register views
    jobs,
    onboarding,
    platform_oauth,
    routes,
    routes_oauth,
    routes_onboarding,
    routes_signup,
    routes_two_step,
    slots,
    two_step,
)
from .blueprint import bp

FEATURE = Feature(
    id="auth",
    name="feature.auth.name",
    description="feature.auth.description",
    toggle="always",
    blueprints=[bp],
    events={"user.created": [onboarding.mark_intro_for_new_user]},
    interceptors={"account.pending_step": two_step.pending_step},
    slots={
        "auth.login.below_form": slots.login_below_form,
        "account.settings_sections": slots.account_settings_sections,
        "admin.user_detail": slots.admin_user_detail,
    },
    jobs=[Job("auth.prune_rate_limits", 3600, jobs.prune_rate_limits),
          Job("auth.platform_signouts", 60, platform_oauth.apply_signouts)],
    order=0,
)
