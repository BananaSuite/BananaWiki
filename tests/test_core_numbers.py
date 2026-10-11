import pytest

from bananawiki.core.numbers import ascii_int
from bananawiki.wiki.markdown import render, video_iframe
from tests.features.pages_support import make_page

SUPERSCRIPT_TWO = "²"
ARABIC_THREE = "٣"


@pytest.mark.parametrize("text, expected", [("0", 0), ("42", 42), ("007", 7)])
def test_ascii_digits_are_parsed(text, expected):
    assert ascii_int(text) == expected


@pytest.mark.parametrize("text", ["", "-1", "1.5", " 1", "1e3", SUPERSCRIPT_TWO, ARABIC_THREE, "9" * 5000, None])
def test_anything_else_is_refused(text):
    assert ascii_int(text) is None


def test_long_numbers_are_kept_for_the_caller_to_range_check():
    assert ascii_int("9" * 25) == 10**25 - 1


def test_the_digit_limit_can_be_lowered():
    assert ascii_int("200", 3) == 200
    assert ascii_int("2000", 3) is None


@pytest.mark.parametrize("margin", [SUPERSCRIPT_TWO, ARABIC_THREE, "9999"])
def test_video_margin_ignores_values_int_cannot_read(margin):
    assert video_iframe("https://youtu.be/dQw4w9WgXcQ", margin=margin) == video_iframe("https://youtu.be/dQw4w9WgXcQ")


def test_video_margin_keeps_a_valid_value():
    assert "120" in video_iframe("https://youtu.be/dQw4w9WgXcQ", margin="120")


def test_page_with_a_superscript_margin_still_renders():
    html = render(f'[[video url="https://youtu.be/dQw4w9WgXcQ" margin="{SUPERSCRIPT_TWO}"]]')
    assert "youtube" in html


def test_sync_with_a_superscript_revision_asks_for_a_reload(app, admin_client):
    page = make_page(app, "Sync target", "text")
    response = admin_client.get(f"/api/page/{page['slug']}/sync?since={SUPERSCRIPT_TWO}")
    assert response.status_code == 200
    assert response.get_json()["reload"] is True


def test_pdf_export_survives_a_non_ascii_list_start(app, db, admin_client):
    db.execute("UPDATE site_settings SET pdf_export_enabled = 1")
    page = make_page(app, "Numbered", f'<ol start="{SUPERSCRIPT_TWO}"><li>one</li></ol>')
    response = admin_client.get(f"/page/{page['slug']}/export-pdf")
    assert response.status_code == 200
