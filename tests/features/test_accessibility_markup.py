"""Markup rules that axe reported as WCAG AA failures."""

import re
from html.parser import HTMLParser

from tests.features.pages_support import make_category, make_page


class _Summaries(HTMLParser):
    """Collects the interactive elements found inside any <summary>."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "summary":
            self.depth += 1
        elif self.depth and tag in ("a", "button", "input", "select", "textarea"):
            self.nested.append(tag)

    def handle_endtag(self, tag):
        if tag == "summary" and self.depth:
            self.depth -= 1


def test_category_rows_hold_no_controls_inside_their_summary(app, admin_client):
    category = make_category(app, "Handbook")
    make_page(app, "Welcome", "text", category_id=category["id"])
    parser = _Summaries()
    parser.feed(admin_client.get("/navigation").get_data(as_text=True))
    assert parser.nested == []


def test_category_row_keeps_its_open_link_and_reorder_buttons(app, admin_client):
    category = make_category(app, "Handbook")
    html = admin_client.get("/navigation").get_data(as_text=True)
    assert f'/category/{category["id"]}' in html
    assert 'data-move="up"' in html and 'data-move="down"' in html


def test_traffic_chart_bars_carry_no_unnamed_focus_stops(admin_client):
    html = admin_client.get("/admin/dashboard").get_data(as_text=True)
    bars = [part for part in html.split('class="traffic-chart__bar"')[1:]]
    assert bars and all("tabindex" not in part.split(">", 1)[0] and "aria-label" not in part.split(">", 1)[0]
                        for part in bars)


def test_editor_preview_is_a_labelled_region(app, admin_client):
    page = make_page(app, "Draft", "text")
    html = admin_client.get(f"/page/{page['slug']}/edit").get_data(as_text=True)
    preview = html.split('id="editor-preview"', 1)[1].split(">", 1)[0]
    assert 'role="region"' in preview and "aria-label=" in preview


def test_user_links_beside_badges_are_underlined(admin_client):
    # Next to the "Superuser" badge the link is part of running text and must not rely on color (WCAG 1.4.1).
    html = admin_client.get("/admin/users").get_data(as_text=True)
    assert re.search(r'<a class="text-link" href="/admin/users/[^"]+">', html)
