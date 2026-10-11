import io

import pytest

from bananawiki.hosting import wikihosts


def _chunked(payload: bytes) -> dict:
    return {"wsgi.input": io.BytesIO(payload), "HTTP_TRANSFER_ENCODING": "chunked", "REQUEST_METHOD": "POST"}


def test_chunked_body_within_the_limit_is_forwarded_whole(monkeypatch):
    monkeypatch.setattr(wikihosts, "MAX_BUFFERED_BODY", 10)
    assert wikihosts._request_body(_chunked(b"0123456789")) == b"0123456789"


def test_chunked_body_over_the_limit_is_refused_instead_of_cut(monkeypatch):
    monkeypatch.setattr(wikihosts, "MAX_BUFFERED_BODY", 10)
    with pytest.raises(wikihosts.BodyTooLarge):
        wikihosts._request_body(_chunked(b"0123456789!"))
