"""BananaWiki 1.4 userbot SDK name, kept for existing scripts.

``from bananawiki_userbot_sdk import UserbotClient`` keeps working; the client
now lives in :mod:`bananawiki.sdk.userbot` (and the general REST client,
``WikiClient``, in :mod:`bananawiki.sdk.client`).
"""

from bananawiki.sdk.userbot import ApiError, UserbotClient, WikiClient, verify_webhook

__all__ = ["ApiError", "UserbotClient", "WikiClient", "verify_webhook"]
