"""The portal's favicon and home-screen icon are the wiki's yellow banana (scripts/make_icons.py)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bananawiki" / "hosting" / "static"
VERSION = r"\?v=[0-9a-f]{10}"


def test_the_portal_links_its_banana_icons_with_a_content_version(web):
    page = web.get("/", follow_redirects=True).get_data(as_text=True)
    head = page.split("<body")[0]
    icon = re.search(rf'<link rel="icon" href="(/static/favicon\.png{VERSION})">', head)
    touch = re.search(rf'<link rel="apple-touch-icon" href="(/static/apple-touch-icon\.png{VERSION})">', head)
    assert icon and touch
    for url, name in ((icon.group(1), "favicon.png"), (touch.group(1), "apple-touch-icon.png")):
        response = web.get(url)
        assert (response.status_code, response.mimetype) == (200, "image/png")
        assert response.data == (STATIC / name).read_bytes()
    # The top bar shows the mascot instead of a logo (test_hosting_mascot).
    assert "topbar__logo" not in page


def test_browsers_asking_for_favicon_ico_get_the_banana(web):
    # Browsers ask for /favicon.ico where a page names no icon (a JSON answer, a download, a bookmark of
    # one); the portal sends the versioned banana, as the wiki and BananaChat do.
    response = web.get("/favicon.ico")
    assert response.status_code == 302
    assert re.fullmatch(rf"/static/favicon\.png{VERSION}", response.headers["Location"])
    icon = web.get(response.headers["Location"])
    assert (icon.status_code, icon.mimetype) == (200, "image/png")
    assert icon.data == (STATIC / "favicon.png").read_bytes()
