"""The mascot in the portal's top bar: the wiki's sprite, script and stylesheet, sunglasses kept in the browser."""

from __future__ import annotations

import re

import pytest

from bananawiki.core import assets, mascot

VERSION = r"\?v=[0-9a-f]{10}"
LABELS = {"en": "BananaWiki mascot", "it": "Mascotte di BananaWiki", "de": "BananaWiki-Maskottchen"}


def _topbar(page: str) -> str:
    return page[page.index('<header class="topbar hosting-topbar">'):page.index("</header>")]


def test_the_mascot_takes_the_logo_s_place_and_the_name_still_links_home(web):
    topbar = _topbar(web.get("/login").get_data(as_text=True))
    button = re.search(r'<button type="button" class="mascot" data-mascot data-mascot-store="local" '
                       r'data-mascot-clicks="(\d+)"\s+aria-label="BananaWiki mascot" title="BananaWiki mascot">'
                       r'<span class="mascot__body">(.*?)</span></button>', topbar, re.S)
    assert button and int(button.group(1)) == mascot.CLICKS_FOR_SHADES
    assert button.group(2) == str(mascot.svg("normal"))
    assert "topbar__logo" not in topbar and "data-mascot-url" not in topbar
    assert '<a class="topbar__brand" href="/"><span>BananaWiki Hosting</span></a>' in topbar
    assert topbar.index("data-mascot") < topbar.index("topbar__brand")


def test_signed_in_pages_show_it_too(web, make_account, login):
    login(web, make_account("mascot_owner"))
    page = web.get("/", follow_redirects=True).get_data(as_text=True)
    assert 'class="mascot" data-mascot data-mascot-store="local"' in _topbar(page)


@pytest.mark.parametrize("language", sorted(LABELS))
def test_the_label_and_the_messages_are_translated(web, language):
    web.post("/language", data={"language": language})
    page = web.get("/login").get_data(as_text=True)
    assert f'aria-label="{LABELS[language]}" title="{LABELS[language]}"' in _topbar(page)
    strings = page[page.index('id="bw-strings">'):]
    for key in ("mascot.label", "mascot.label_shades", "mascot.unlocked_local", "mascot.shades_off"):
        assert f'"{key}":' in strings[:strings.index("</script>")]
    assert f'"mascot.label":"{LABELS[language]}"' in strings


def test_the_script_and_stylesheet_are_the_wiki_s_and_load_for_visitors(web):
    page = web.get("/login").get_data(as_text=True)
    css = re.search(rf'<link rel="stylesheet" href="(/static/mascot/mascot\.css{VERSION})">', page)
    js = re.search(rf'<script src="(/static/mascot/mascot\.js{VERSION})"></script>', page)
    assert css and js
    for url, name, mimetype in ((css.group(1), "mascot.css", "text/css"), (js.group(1), "mascot.js", "text/javascript")):
        response = web.get(url)
        assert (response.status_code, response.mimetype) == (200, mimetype)
        assert response.data == (assets.SHARED_STATIC_ROOT / "mascot" / name).read_bytes()
    # After the portal's own script, which provides the strings and toasts the mascot uses.
    assert page.index("js/hosting.js") < page.index("mascot/mascot.js")


def test_the_mascot_stands_still_when_less_motion_is_asked():
    css = (assets.SHARED_STATIC_ROOT / "mascot" / "mascot.css").read_text(encoding="utf-8")
    block = css[css.rindex("@media (prefers-reduced-motion: reduce)"):]
    assert ".mascot__body" in block and ".mascot-sprite .mascot__eyes" in block and "animation: none" in block


def test_only_page_not_found_has_the_mascot_peeking_in(web):
    missing = web.get("/no-such-page")
    assert missing.status_code == 404
    assert '<span class="mascot-peek" aria-hidden="true"><svg class="mascot-sprite"' in missing.get_data(as_text=True)
    assert "mascot-peek" not in web.get("/login").get_data(as_text=True)
