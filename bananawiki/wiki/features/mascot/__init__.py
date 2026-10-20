"""The mascot: a pixel-art banana in place of the top bar logo.

Every signed-in person sees it while the feature is on: only switching the
plugin off brings back the static logo. It bobs, blinks and hops when clicked;
the eleventh click puts sunglasses on it, and they stay until the person clicks
eleven more times or takes them off under "Customise". Administrators can put
them on or take them off for everyone at once from Appearance. See
:mod:`.service` for where they are stored. The Konami code sends a shower of
small bananas, and on the "page not found" card the mascot peeks in.

Slots filled here
-----------------
* ``topbar.brand`` - the mascot in place of the logo, with its stylesheet and script, for
  signed-in people.
* ``account.display_sections`` - "Take the sunglasses off" under "Customise", only while they are on.
* ``admin.appearance`` - the sunglasses actions for every account.
* ``error.card`` - the mascot peeking into the "page not found" card.
"""

from __future__ import annotations

from flask import render_template

from ....core import mascot as sprite
from ... import auth
from ...registry import Feature
from . import service
from .routes import bp


def topbar() -> str:
    state = service.state(auth.current_user())
    if not state["enabled"]:
        return ""
    shades = state["shades"]
    return render_template("mascot/_topbar.html", sprite=sprite.svg("shades" if shades else "normal"),
                           shades=shades, clicks=sprite.CLICKS_FOR_SHADES)


def display_section() -> str:
    # Only the sunglasses can be changed here: without them the section would be empty.
    if not service.state(auth.current_user())["shades"]:
        return ""
    return render_template("mascot/_display.html", sprite=sprite.svg)


def error_card(code: int | str = 0) -> str:
    state = service.state(auth.current_user())
    if not state["enabled"] or str(code) != "404":
        return ""
    return render_template("mascot/_peek.html", sprite=sprite.svg("shades" if state["shades"] else "normal"))


def admin_section() -> str:
    return render_template("mascot/_admin.html", sprite=sprite.svg)


FEATURE = Feature(
    id="mascot",
    name="feature.mascot.name",
    description="feature.mascot.description",
    toggle="plugin",
    blueprints=[bp],
    slots={
        "topbar.brand": topbar,
        "account.display_sections": display_section,
        "admin.appearance": admin_section,
        "error.card": error_card,
    },
)
