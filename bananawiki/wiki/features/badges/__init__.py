"""Badges: awards shown on profiles, given by administrators or earned automatically.

Automatic triggers (first edit, edit count, categories, pages created, days
of membership, minutes spent reading) are evaluated by the ``badges.evaluate``
job and, for the author, when a page is saved or they sign in.
"""

from __future__ import annotations

from flask import render_template, request

from ... import auth
from ...registry import Feature, Job, NavItem
from . import service
from .routes import bp


def _count(user) -> int:
    return service.unnotified_count(user["id"]) if user else 0


def _profile_sections(profile_user) -> str:
    badges = service.user_badges(profile_user["id"])
    return render_template("badges/_profile.html", badges=badges) if badges else ""


def _banner() -> str:
    user = auth.current_user()
    if user is None or request.endpoint and request.endpoint.startswith("badges."):
        return ""
    count = service.unnotified_count(user["id"])
    return render_template("badges/_banner.html", count=count) if count else ""


def _reading_tracker(page) -> str:
    if auth.current_user() is None or not service.reading_tracked():
        return ""
    return render_template("badges/_reading.html")


FEATURE = Feature(
    id="badges",
    name="feature.badges.name",
    description="feature.badges.description",
    toggle="plugin",
    blueprints=[bp],
    nav=[
        NavItem("badges.nav.notifications", "badges.notifications", icon="award", area="user", order=30,
                visible=lambda user: _count(user) > 0, badge=_count),
        NavItem("badges.admin.nav", "badges.admin_list", icon="award", area="admin", order=64,
                visible=lambda user: bool(user) and auth.is_admin(user)),
    ],
    jobs=[Job("badges.evaluate", 900, service.evaluate_everyone, initial_delay=120)],
    events={
        "page.created": [service.on_page_saved],
        "page.updated": [service.on_page_saved],
        "user.login": [service.on_login],
    },
    slots={
        "profile.sections": _profile_sections,
        "page.banners": _banner,
        "page.below_content": _reading_tracker,
    },
    order=14,
)
