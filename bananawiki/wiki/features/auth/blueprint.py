"""The sign-in blueprint (always on). Endpoints are named ``auth.*``."""

from flask import Blueprint

bp = Blueprint(
    "auth",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static/auth",
)

# Gates every sign-in page must pass through (the pages resolve those states).
ALL_GATES = ("setup", "maintenance", "account_steps", "approval")
# Pages reachable while signing in: after setup, during maintenance (so
# administrators can get in), and while the account owes a step or approval.
SIGN_IN_GATES = ("maintenance", "account_steps", "approval")
