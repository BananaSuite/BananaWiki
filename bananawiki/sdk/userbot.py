"""The userbot client: :class:`~bananawiki.sdk.client.WikiClient` plus the userbot endpoints.

It has no dependencies, so it can be copied into a script on another machine
(together with :mod:`bananawiki.sdk.client`)::

    from bananawiki.sdk.userbot import UserbotClient

    bot = UserbotClient("https://wiki.example.org", "YOUR_USERBOT_KEY")
    print(bot.me())
    bot.update_profile(real_name="Bot", bio="Automated", page_published=True)

Every other endpoint is reachable with :meth:`UserbotClient.request` or the
helpers of :class:`~bananawiki.sdk.client.WikiClient`, for a key or token
that holds the matching scope. Errors raise :class:`ApiError` with the API's
stable ``code``.
"""

from __future__ import annotations

from typing import Any

from .client import MAX_RESPONSE, ApiError, WikiClient, verify_webhook

__all__ = ["MAX_RESPONSE", "ApiError", "UserbotClient", "WikiClient", "verify_webhook"]


class UserbotClient(WikiClient):
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0):
        super().__init__(base_url, api_key, timeout=timeout)

    def me(self) -> dict[str, Any]:
        """Account, profile and key metadata of the userbot."""
        return self.request("GET", "/userbot/me")

    def update_profile(self, *, real_name: str = "", bio: str = "", page_published: bool | None = None):
        payload: dict[str, Any] = {"real_name": real_name, "bio": bio}
        if page_published is not None:
            payload["page_published"] = bool(page_published)
        return self.request("POST", "/userbot/profile", payload)
