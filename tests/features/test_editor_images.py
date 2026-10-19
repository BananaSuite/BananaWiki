"""The editor's image options: what the dialog writes survives rendering as written."""

from __future__ import annotations

import pytest

from bananawiki.wiki import markdown

SRC = "/static/uploads/5de18075a7caa71f1c553d38c4b58f2e.jpg"


@pytest.mark.parametrize("align", ["left", "right", "center", "full"])
def test_aligned_image_keeps_its_class_width_and_spacing(align):
    html = markdown.render(
        f'<img src="{SRC}" alt="Luca" width="320" class="media-{align}" style="padding:12px">\n'
        "The text that follows."
    )
    assert f'class="media-{align}"' in html and 'width="320"' in html and 'style="padding:12px"' in html, html


def test_text_after_a_floated_image_shares_its_paragraph():
    # The float only lets the text run beside the picture inside the same block.
    html = markdown.render(f'<img src="{SRC}" alt="Luca" class="media-right">\nNice to add **alt text**')
    paragraph = html[html.index("<p>"):html.index("</p>")]
    assert "media-right" in paragraph and "<strong>alt text</strong>" in paragraph, html


def test_percentage_width_is_kept():
    html = markdown.render(f'<img src="{SRC}" alt="" width="50%">')
    assert 'width="50%"' in html, html


def test_escaped_alt_text_stays_inside_its_attribute():
    alt = "A &quot;rabbit&quot; &lt;b&gt;here&lt;/b&gt; &amp; there"
    html = markdown.render(f'<img src="{SRC}" alt="{alt}">')
    assert "<b>" not in html and "rabbit" in html and "onerror" not in html, html


def test_editor_has_the_image_dialog(admin_client):
    page = admin_client.get("/create").get_data(as_text=True)
    assert '<dialog id="dlg-image"' in page and "data-image-alt" in page and "data-crop-box" in page
    assert 'data-md="image"' in page
    # The dialog's messages reach the script.
    assert '"pages.crop_hint"' in page and '"pages.image_not_found"' in page
