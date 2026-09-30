"""Sign-in: setup, password and portal sign-in, sign-up, account status, onboarding and the tour."""

from ...registry import Feature, Job
from . import jobs, onboarding, routes, routes_oauth, routes_onboarding, routes_signup, slots  # noqa: F401
from .blueprint import bp

FEATURE = Feature(
    id="auth",
    name="feature.auth.name",
    description="feature.auth.description",
    toggle="always",
    blueprints=[bp],
    events={"user.created": [onboarding.mark_intro_for_new_user]},
    slots={
        "auth.login.below_form": slots.login_below_form,
        "account.settings_sections": slots.account_settings_sections,
    },
    jobs=[Job("auth.prune_rate_limits", 3600, jobs.prune_rate_limits)],
    order=0,
)
