"""Page embeds: ``[[kanban board="<id>"]]`` becomes a read-only mini board.

``markdown.render`` turns the shortcode into
``<div class="bw-embed bw-embed-kanban" data-embed-ref="<id>">``; the script
added to every page fills those placeholders from ``/api/embed/kanban/<id>``,
which applies the board's read check, and keeps them current.
"""

from __future__ import annotations

from flask import render_template
from markupsafe import Markup


def page_scripts() -> Markup:
    return Markup(render_template("kanban/_embed_script.html"))
