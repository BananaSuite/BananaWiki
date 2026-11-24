"""The mascot: a pixel-art banana in place of the top bar logo.

Every signed-in person sees it while the feature is on: only switching the
plugin off brings back the static logo. It bobs, blinks and hops when clicked;
the eleventh click puts sunglasses on it, and they stay until the person clicks
eleven more times or takes them off under "Customise". Administrators can put
them on or take them off for everyone at once from Appearance. See
:mod:`.service` for where they are stored. Key sequences typed anywhere play
the key-sequence easter eggs administrators design under Appearance (the
Konami code's shower of mascots by default, see :mod:`.easter`; everyone sees
them, signed in or not), and on the "page not found" card the mascot peeks in.
Each of these easter eggs can be switched off under Admin -> Appearance
(:func:`.service.eggs`); switching the plugin off removes them all.

Slots filled here
-----------------
* ``topbar.brand`` - the mascot in place of the logo, with its stylesheet and script, for
  signed-in people.
* ``account.display_sections`` - "Take the sunglasses off" under "Customise", only while they are on.
* ``admin.appearance`` - the sunglasses actions for every account, the easter egg switches and the editor
  of the key-sequence eggs.
* ``admin.user_detail`` - the eggs an account found, and removing them.
* ``error.card`` - the mascot peeking into the "page not found" card.
* ``page.head`` - the key-sequence eggs (``#banana-eggs``) with their script and stylesheet, on every page.
"""

from __future__ import annotations

from flask import g, render_template, url_for
from markupsafe import Markup

from ....core import eggs as shared
from ....core import mascot as sprite
from ....core.web import CSRF_HEADERS, csrf_token
from ... import auth
from ...i18n import t
from ...registry import Feature
from ...templating import accessibility_prefs
from . import easter, service
from .routes import bp, user_section


def topbar() -> str:
    state = service.state(auth.current_user())
    if not state["enabled"]:
        return ""
    shades = state["shades"]
    eggs = service.eggs()
    return render_template("mascot/_topbar.html", sprite=sprite.svg("shades" if shades else "normal"),
                           shades=shades, clicks=sprite.CLICKS_FOR_SHADES if eggs["sunglasses"] else 0)


def head() -> str:
    """The key-sequence eggs for banana-eggs.js, in <head> before any page content; nothing while they are off."""
    playable = easter.playable()
    if not playable:
        return ""
    user = auth.current_user()
    signed_in = user is not None and not auth.is_impersonating()
    found_url = url_for("mascot.egg_found", egg_id="EGG").replace("/EGG/", "/{id}/")
    config = shared.client_config(
        {"eggs": playable}, found_url=found_url if signed_in else None,
        page_url=url_for("mascot.easter_egg_page"), signed_in=signed_in,
        csrf_header=CSRF_HEADERS[0] if signed_in else None, csrf_token=csrf_token() if signed_in else "",
        reduce_motion=accessibility_prefs().reduce_motion,
        strings={"found": t("mascot.eggs.found"), "open_page": t("mascot.eggs.open_page")})
    # script_json escapes <, > and & so the JSON cannot end the element early.
    return render_template("mascot/_eggs_head.html", config=Markup(shared.script_json(config)))


def display_section() -> str:
    # Only the sunglasses can be changed here: without them the section would be empty.
    if not service.state(auth.current_user())["shades"]:
        return ""
    return render_template("mascot/_display.html", sprite=sprite.svg)


def error_card(code: int | str = 0) -> str:
    state = service.state(auth.current_user())
    if not state["enabled"] or str(code) != "404" or not service.eggs()["peek"]:
        return ""
    return render_template("mascot/_peek.html", sprite=sprite.svg("shades" if state["shades"] else "normal"))


def admin_section() -> str:
    eggs = service.eggs()
    submitted = g.get("easter_egg_submitted")
    options = {
        "strings": {name: t(f"mascot.eggs.editor.{name}", n="{n}", s="{s}") for name in EDITOR_STRINGS},
        "emojis": shared.EMOJIS, "badges": easter.badge_choices(), "mascot": True, "egg": NEW_EGG,
        "reset": shared.default_config()["eggs"], "buttonClass": "btn btn--small",
    }
    return render_template("mascot/_admin.html", sprite=sprite.svg, eggs=eggs, egg_ids=service.EGGS,
                           clicks=sprite.CLICKS_FOR_SHADES, egg_list=submitted or easter.stored_text(),
                           egg_errors=g.get("easter_egg_errors") or [], editor=Markup(shared.script_json(options)))


# The editor's labels (SPEC.md of banana-eggs/v1), translated as mascot.eggs.editor.<name>.
EDITOR_STRINGS = ("title", "add", "remove", "preview", "reset", "record", "stop", "konami", "keys", "keys_hint",
                  "keys_empty", "key_space", "id", "enabled", "sprite", "sprite_emoji", "sprite_mascot", "emoji",
                  "emoji_pick", "count", "speed_min", "speed_max", "delay", "unlock", "unlock_page", "unlock_toast",
                  "unlock_none", "unlock_delay", "record_find", "badge", "badge_none", "message", "limit")
# What "Add" starts from: a banana shower that shows a message, keys still to record.
NEW_EGG = {"enabled": True, "keys": [], "sprite": {"type": "emoji", "value": shared.BANANA}, "count": 30,
           "speed_min": 1.5, "speed_max": 3.5, "delay": 80, "unlock": {"action": "toast", "delay": 2500},
           "record": True, "badge": None, "message": ""}


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
        "admin.user_detail": user_section,
        "error.card": error_card,
        "page.head": head,
    },
)
