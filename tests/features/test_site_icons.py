"""The wiki's favicon, home-screen icon and logo are the mascot's banana (scripts/make_icons.py).

Each icon URL carries a content version, so browsers that kept the earlier
icon fetch the new one. The logo is shown at 32x32, twice the sprite, and
browsers scale it without smoothing; the admin's favicon presets are drawn
the same way, uploaded icons keep the browser's usual scaling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bananawiki.wiki.templating import FAVICON_PRESETS

from .people_support import set_feature

ROOT = Path(__file__).resolve().parents[2]
FAVICONS = ROOT / "bananawiki" / "wiki" / "static" / "favicons"
VERSION = r"\?v=[0-9a-f]{10}"
LOGO = rf'<img class="topbar__logo" src="/static/favicons/banana_yellow\.png{VERSION}" alt="" width="32" height="32">'


def _head(html: str) -> str:
    return html.split("<body")[0]


def test_signed_out_pages_link_the_banana_icons_with_a_content_version(client):
    html = client.get("/login").get_data(as_text=True)
    icon = re.search(rf'<link rel="icon" href="(/static/favicons/banana_yellow\.png{VERSION})">', _head(html))
    touch = re.search(rf'<link rel="apple-touch-icon" href="(/static/favicons/apple-touch-icon\.png{VERSION})">',
                      _head(html))
    assert icon and touch
    for url, name in ((icon.group(1), "banana_yellow.png"), (touch.group(1), "apple-touch-icon.png")):
        response = client.get(url)
        assert (response.status_code, response.mimetype) == (200, "image/png")
        assert response.data == (FAVICONS / name).read_bytes()
    # The sign-in page's bare layout shows the logo above the form.
    assert re.search(LOGO, html)


def test_signed_in_without_the_mascot_the_top_bar_shows_the_logo(app, client, make_user, login):
    set_feature(app, "mascot", False)
    login(client, make_user("logo_viewer"))
    html = client.get("/", follow_redirects=True).get_data(as_text=True)
    assert 'class="topbar"' in html and len(re.findall(LOGO, html)) == 1


def test_a_custom_page_in_its_sandbox_links_the_home_screen_icon_too(client, db):
    # The frame around an author's HTML has a head of its own: it linked the favicon but not the
    # home-screen icon, so a phone saving the page asked for /apple-touch-icon.png, which no app serves.
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    db.insert("custom_pages", {"path": "/landing", "title": "T", "content_type": "html", "content": "<h1>Hi</h1>",
                               "is_published": 1})
    html = client.get("/landing").get_data(as_text=True)
    assert 'class="cp-sandbox"' in html
    head = _head(html)
    assert re.search(rf'<link rel="icon" href="/static/favicons/banana_yellow\.png{VERSION}">', head)
    assert re.search(rf'<link rel="apple-touch-icon" href="/static/favicons/apple-touch-icon\.png{VERSION}">', head)


@pytest.mark.parametrize("preset", FAVICON_PRESETS)
def test_the_chosen_preset_is_the_versioned_favicon(admin_client, preset):
    assert admin_client.post("/admin/settings/favicon/select", json={"favicon_type": preset}).status_code == 200
    head = _head(admin_client.get("/admin/appearance").get_data(as_text=True))
    assert re.search(rf'<link rel="icon" href="/static/favicons/banana_{preset}\.png{VERSION}">', head)
    # The home-screen icon stays the yellow banana on its tile.
    assert re.search(rf'<link rel="apple-touch-icon" href="/static/favicons/apple-touch-icon\.png{VERSION}">', head)


def test_the_favicon_is_the_standard_banana_while_switched_off(admin_client):
    admin_client.post("/admin/settings/favicon/select", json={"favicon_type": "red"})
    admin_client.post("/admin/appearance/favicon/enabled", data={"favicon_enabled": "0"})
    head = _head(admin_client.get("/admin/appearance").get_data(as_text=True))
    assert re.search(rf'<link rel="icon" href="/static/favicons/banana_yellow\.png{VERSION}">', head)


def test_the_presets_are_drawn_crisp_and_uploaded_icons_are_not(admin_client):
    page = admin_client.get("/admin/appearance").get_data(as_text=True)
    presets = re.findall(rf'<img class="favicon-tile__preset" src="/static/favicons/banana_(\w+)\.png{VERSION}" '
                         r'alt="" width="32" height="32">', page)
    assert presets == list(FAVICON_PRESETS)
    css = (ROOT / "bananawiki" / "wiki" / "features" / "site_admin" / "static" / "site_admin.css").read_text("utf-8")
    assert re.search(r"\.favicon-tile__preset \{[^}]*image-rendering: pixelated;", css)
    assert not re.search(r"\.favicon-tile img \{[^}]*image-rendering", css)


def test_every_banana_logo_is_twice_the_sprite_and_versioned():
    """Templates of the wiki and the portal: each banana <img> is 32x32 and its URL follows the file."""
    found = []
    for path in sorted((ROOT / "bananawiki").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        found += [(path.relative_to(ROOT).as_posix(), tag)
                  for tag in re.findall(r"<img[^>]*(?:favicons/banana_|favicon\.png|apple-touch)[^>]*>", text)]
    assert {path for path, _tag in found} >= {
        "bananawiki/wiki/templates/base.html", "bananawiki/wiki/templates/base_minimal.html",
        "bananawiki/wiki/features/custom_pages/templates/custom_pages/_page.html",
        "bananawiki/wiki/features/site_admin/templates/site_admin/appearance.html",
        "bananawiki/hosting/templates/hosting/base.html"}
    wrong = [(path, tag) for path, tag in found
             if 'width="32" height="32"' not in tag or "asset_url(" not in tag or "url_for('static'" in tag]
    assert wrong == []


def test_logos_stay_crisp_when_the_browser_scales_them():
    """At 32 CSS pixels a high-density screen doubles the 32x32 file again: without smoothing, every sprite
    pixel stays a square (the portal shares the rule and does not resize the logo)."""
    css = (ROOT / "bananawiki" / "core" / "static" / "css" / "bananawiki.css").read_text(encoding="utf-8")
    assert re.search(r"\.topbar__brand img \{ width: 32px; height: 32px; image-rendering: pixelated; \}", css)
    hosting = (ROOT / "bananawiki" / "hosting" / "static" / "css" / "hosting.css").read_text(encoding="utf-8")
    assert not re.search(r"\.topbar__brand img \{", hosting)


def test_on_phones_the_logo_leaves_the_site_name_the_room_the_mascot_does():
    """mascot.css pulls the 32px mascot 5px into the menu button's gap on phones, so the site name starts where
    it started next to the earlier 23px logo. The 32px logo next to the menu button does the same (its empty
    column and three more pixels, on either side for right-to-left) and its gap before the name narrows by
    .25rem: the name starts in the same place whether the mascot or the logo is shown."""
    css = (ROOT / "bananawiki" / "core" / "static" / "css" / "bananawiki.css").read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 600px) {\n  .topbar { padding-inline: .65rem; gap: .4rem; }")[1].split("\n}")[0]
    # The top bar's gap (.4rem) less the mascot's pull towards the name (.25rem) is the logo's gap.
    assert ".shell > .topbar > .topbar__brand { flex: 1 1 auto; gap: .4rem; font-size: .9rem; }" in phone
    assert ".shell > .topbar > .topbar__menu-toggle + .topbar__brand { gap: .15rem; }" in phone
    assert ".shell > .topbar > .topbar__menu-toggle + .topbar__brand img { margin-inline-start: -5px; }" in phone
    mascot = (ROOT / "bananawiki" / "wiki" / "features" / "mascot" / "static" / "mascot.css").read_text("utf-8")
    assert re.search(r"@media \(max-width: 600px\) \{\s*\.mascot \{ margin-left: -5px; \}", mascot)
    assert ".mascot + .topbar__brand { margin-inline-start: -.25rem; }" in mascot
