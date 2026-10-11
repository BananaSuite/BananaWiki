"""Kept PDFs follow the uploaded images they embed (second 1.7 audit).

A PDF embeds images of the wiki's ``uploads`` folder read at layout time, so
the cache key covers each embedded file: a deleted (or replaced) upload is
never served again from a PDF made before.
"""

from __future__ import annotations

import io

from PIL import Image

from bananawiki.wiki import storage
from bananawiki.wiki.features.page_export import pdf

IMAGE = b"/Subtype /Image"  # how an embedded picture appears in the PDF


def _png(path, size, colour):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())


def test_a_kept_pdf_is_made_again_when_an_embedded_upload_changes(app, monkeypatch):
    calls = []
    original = pdf._render
    monkeypatch.setattr(pdf, "_render", lambda **kwargs: calls.append(1) or original(**kwargs))
    with app.test_request_context():
        image = storage.folder_path("uploads") / "5f0c2a9e1d7b4c3a8e6f.png"
        _png(image, (40, 30), "red")
        options = {"title": "Pictures", "content": f"# Pictures\n\n![diagram](/static/uploads/{image.name})\n\nText",
                   "site_name": "Wiki", "base_url": "https://wiki.example/", "meta": ["by someone"]}
        first = pdf.render(**options)
        assert pdf.render(**options) == first and len(calls) == 1

        _png(image, (80, 60), "blue")  # replaced under the same name
        replaced = pdf.render(**options)
        assert len(calls) == 2 and replaced != first

        image.unlink()  # deleted, e.g. for privacy reasons
        gone = pdf.render(**options)
        # The picture is gone from the new PDF. (Not compared by size: with the fonts of other machines the
        # alt text's glyphs can outweigh a small picture.)
        assert len(calls) == 3 and IMAGE in first and IMAGE not in gone
        assert pdf.render(**options) == gone and len(calls) == 3
