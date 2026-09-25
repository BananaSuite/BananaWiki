"""
BananaWiki: Per-request ``g`` caching helpers.

Each function wraps an expensive ``db.*`` call and caches the result on
Flask's ``g`` object for the lifetime of the current request.  Callers that
use these helpers instead of the raw ``db.*`` functions will never issue the
same query more than once per request, even if the value is needed both in a
route handler and in the ``inject_globals`` context processor.
"""

from flask import g

import db


def get_request_category_tree():
    """Return ``(tree, uncategorized)`` cached on ``g`` for this request."""
    if not hasattr(g, "_category_tree"):
        g._category_tree = db.get_category_tree(include_content=False)
    return g._category_tree


def get_request_list_categories():
    """Return the flat category list cached on ``g`` for this request."""
    if not hasattr(g, "_list_categories"):
        g._list_categories = db.list_categories()
    return g._list_categories


def get_request_user_accessibility(user_id):
    """Return user accessibility preferences cached on ``g`` for this request."""
    cache = getattr(g, "_user_accessibility_cache", None)
    if cache is None:
        cache = {}
        g._user_accessibility_cache = cache
    if user_id not in cache:
        cache[user_id] = db.get_user_accessibility(user_id)
    return cache[user_id]




def get_request_sidebar_people(user=None):
    """Return sidebar people visible to the current viewer, cached per request."""
    if not hasattr(g, "_sidebar_people"):
        role = (user or {}).get("role") if user else None
        people = db.list_published_profiles(limit=19, include_bio=False)
        if not people and user:
            profile = db.get_user_profile(user["id"])
            people = [{
                "id": user["id"],
                "username": user["username"],
                "role": role or "user",
                "suspended": user.get("suspended", 0),
                "userbot_enabled": user.get("userbot_enabled", 0),
                "real_name": profile["real_name"] if profile else "",
                "bio": profile["bio"] if profile else "",
                "avatar_filename": profile["avatar_filename"] if profile else "",
                "page_published": profile["page_published"] if profile else 0,
                "page_disabled_by_admin": profile["page_disabled_by_admin"] if profile else 0,
            }]
        g._sidebar_people = people[:19]
    return g._sidebar_people


def get_request_unread_dm_count(user_id):
    """Return total unread DM count cached on ``g`` for this request."""
    cache = getattr(g, "_unread_dm_cache", None)
    if cache is None:
        cache = {}
        g._unread_dm_cache = cache
    if user_id not in cache:
        cache[user_id] = db.get_total_unread_dm_count(user_id)
    return cache[user_id]


def get_request_unread_group_count(user_id):
    """Return total unread group message count cached on ``g`` for this request."""
    cache = getattr(g, "_unread_group_cache", None)
    if cache is None:
        cache = {}
        g._unread_group_cache = cache
    if user_id not in cache:
        cache[user_id] = db.get_total_unread_group_count(user_id)
    return cache[user_id]


def get_request_reservations_map(user_id, page_ids=None):
    """Return active page reservations map cached on ``g`` for this request."""
    cache = getattr(g, "_reservations_map_cache", None)
    if cache is None:
        cache = {}
        g._reservations_map_cache = cache
    key = user_id if page_ids is None else (user_id, tuple(sorted(page_ids)))
    if key not in cache:
        cache[key] = db.get_active_page_reservations_map(user_id, page_ids)
    return cache[key]
