"""The mascot: a pixel-art banana in place of the top bar logo.

It bobs, blinks and hops when clicked; the eleventh click puts sunglasses on
it, and they stay until the person takes them off under "Customize".
Administrators can show, hide or dress it for everyone at once from
Appearance. See :mod:`.service` for where the choices are stored.

Slots filled here
-----------------
* ``topbar.brand`` - the mascot in place of the logo, with its stylesheet and script, for
  signed-in people who keep it on.
* ``account.display_sections`` - the viewer's own switch under "Customize".
* ``admin.appearance`` - the actions for every account.
"""

from __future__ import annotations

from flask import render_template

from ... import auth
from ...registry import Feature
from . import service, sprite
from .routes import bp


def topbar() -> str:
    state = service.state(auth.current_user())
    if not state["enabled"]:
        return ""
    shades = state["shades"]
    return render_template("mascot/_topbar.html", sprite=sprite.svg("shades" if shades else "normal"),
                           shades=shades, clicks=service.CLICKS_FOR_SHADES)


def display_section() -> str:
    user = auth.current_user()
    if user is None:
        return ""
    return render_template("mascot/_display.html", state=service.state(user), sprite=sprite.svg,
                           current=service.variant(user))


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
    },
)
