"""Page navigation avoids full bodies while retaining publication permissions."""
import tracemalloc

import db
from helpers import filter_visible_navigation
from helpers._request_cache import get_request_category_tree


def test_navigation_keeps_builder_privacy_without_loading_large_bodies(client, admin_user):
    body = "Large page body. " * 150000
    hidden = db.create_page("Private builder", "private-builder", body, user_id=admin_user)
    visible = db.create_page("Public builder", "public-builder", body, user_id=admin_user)
    with db.get_db_context() as connection:
        connection.execute("UPDATE pages SET builder_json='{}', builder_public=0 WHERE id=?", (hidden,))
        connection.execute("UPDATE pages SET builder_json='{}', builder_public=1 WHERE id=?", (visible,))
        connection.commit()
    db.update_site_settings(public_mode=1)
    with client.application.test_request_context('/'):
        tracemalloc.start()
        try:
            tree, uncategorized = get_request_category_tree()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 1024 * 1024
        navigation = filter_visible_navigation(tree, uncategorized, None)
        assert [page['id'] for page in navigation['uncategorized']] == [visible]
    # Existing export callers retain the complete document.
    _, complete = db.get_category_tree()
    assert {page['id']: page['content'] for page in complete}[hidden] == body
