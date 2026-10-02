"""Malformed URL authorities must be refused as normal validation errors."""

import pytest

from bananawiki.wiki.features.attention import service as attention
from bananawiki.wiki.features.custom_pages import service as custom_pages
from bananawiki.wiki.features.federation import protocol
from bananawiki.wiki.features.page_builder import document
from bananawiki.wiki.features.user_profiles import service as profiles


@pytest.mark.parametrize("url", ["https://[", "https://[not-an-ipv6-address]", "https://example.org\uff1a443/"])
def test_malformed_urls_are_refused_consistently(url):
    with pytest.raises(document.DocumentError, match="page_builder.error.link"):
        document.validate({"version": 2, "blocks": [{"type": "button", "label": "Open", "url": url}]})
    with pytest.raises(document.DocumentError, match="page_builder.error.youtube"):
        document.validate({"version": 2, "blocks": [{"type": "youtube", "url": url}]})
    with pytest.raises(profiles.ProfileFieldError, match="profiles.error.url_invalid"):
        profiles.clean_value("url", url)
    with pytest.raises(protocol.ProtocolError, match="federation.error.base_url"):
        protocol.base_url(url)
    with pytest.raises(attention.NotificationError, match="attention.admin.error.base_url"):
        attention.clean_base_url(url)
    assert custom_pages.is_safe_redirect(url) is False
    assert custom_pages.is_safe_link(url) is False
    # Stored links from an upgrade are validated again while rendering.
    assert custom_pages.parse_links('[{"title":"Bad","url":"' + url + '"}]') == []
