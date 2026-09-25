"""Large sidebars keep navigation complete without rendering every page."""

import html
import re
import tracemalloc

import db
import pytest


def page_ids(document):
    return [int(value) for value in re.findall(r'data-page-id="(\d+)"', document)]


def more_url(document):
    match = re.search(r'data-sidebar-load="([^"]+)"', document)
    return html.unescape(match.group(1)) if match else None


def test_large_sidebar_pages_all_links_without_duplicates(admin_user, logged_in_admin):
    with db.get_db_context() as connection:
        connection.executemany(
            "INSERT INTO pages(title,slug,content,last_edited_by,sort_order) VALUES(?,?,?,?,?)",
            [("Same title", f"paged-{i}", "Page text", admin_user, 0) for i in range(251)],
        )
        connection.commit()
        expected = [row[0] for row in connection.execute("SELECT id FROM pages WHERE is_home=0 ORDER BY sort_order,title,id")]
    response = logged_in_admin.get("/page/paged-250")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    seen = page_ids(text)
    assert len(seen) == 100
    assert len(response.data) < 600000
    assert 'sidebar-current-page' in text
    next_url = more_url(text)
    batches = 0
    while next_url:
        response = logged_in_admin.get(next_url)
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'private, no-store'
        text = response.json['html']
        batch = page_ids(text)
        assert 0 < len(batch) <= 50
        seen.extend(batch)
        next_url = more_url(text)
        batches += 1
        assert batches <= 4
    assert seen == expected


def test_public_navigation_omits_private_builders_and_hidden_pages(client, admin_user):
    db.update_site_settings(public_mode=1)
    ordinary = db.create_page("Ordinary", "ordinary", user_id=admin_user)
    public = db.create_page("Public builder", "public-builder-nav", user_id=admin_user, builder_json='{}', builder_public=True)
    private = db.create_page("Private builder", "private-builder-nav", user_id=admin_user, builder_json='{}')
    deleted = db.create_page("Hidden deletion", "hidden-deletion-nav", user_id=admin_user)
    deindexed = db.create_page("Deindexed", "deindexed-nav", user_id=admin_user)
    with db.get_db_context() as connection:
        connection.execute("UPDATE pages SET pending_deletion=1 WHERE id=?", (deleted,))
        connection.execute("UPDATE pages SET is_deindexed=1 WHERE id=?", (deindexed,))
        connection.commit()
    response = client.get('/api/sidebar/pages')
    assert response.status_code == 200
    assert set(page_ids(response.json['html'])) == {ordinary, public}
    assert {private, deleted, deindexed}.isdisjoint(page_ids(response.json['html']))
    assert client.get('/navigation/pages').status_code == 200


def test_permission_revocation_applies_to_subsequent_batches(admin_user, editor_user, logged_in_editor):
    category = db.create_category("Scoped category")
    db.create_page("Scoped page", "scoped-page", category_id=category, user_id=admin_user)
    response = logged_in_editor.get(f'/api/sidebar/pages?category_id={category}')
    assert response.status_code == 200 and 'Scoped page' in response.json['html']
    db.set_user_permissions(editor_user, {'page.delete'}, read_restricted=True,
                            read_category_ids=[], write_restricted=True, write_category_ids=[])
    response = logged_in_editor.get(f'/api/sidebar/pages?category_id={category}')
    assert response.status_code == 404
    assert b'Scoped page' not in response.data


@pytest.mark.parametrize('query', ['category_id=-1', 'category_id=9223372036854775808',
                                  'after=9223372036854775808', 'after=invalid'])
def test_navigation_rejects_invalid_cursors(logged_in_admin, query):
    assert logged_in_admin.get('/api/sidebar/pages?' + query).status_code == 400


def test_navigation_rejects_removed_anchor(admin_user, logged_in_admin):
    removed = db.create_page('Removed anchor', 'removed-anchor', user_id=admin_user)
    with db.get_db_context() as connection:
        connection.execute('DELETE FROM pages WHERE id=?', (removed,))
        connection.commit()
    assert logged_in_admin.get(f'/api/sidebar/pages?after={removed}').status_code == 409


@pytest.mark.parametrize('ids', [[1, 1], [-1], [2**63], [float('inf')]])
def test_page_reorder_rejects_invalid_ids(logged_in_admin, ids):
    assert logged_in_admin.post('/api/reorder/pages', json={'ids': ids}).status_code == 400


def test_category_controls_are_loaded_on_demand(admin_user, logged_in_admin):
    category = db.create_category("Manage lazily")
    for i in range(3):
        db.create_page(f"Category page {i}", f"cat-page-{i}", category_id=category, user_id=admin_user)
    response = logged_in_admin.get('/')
    assert response.status_code == 200
    assert f'data-open-cat-modal="catManageModal{category}"'.encode() in response.data
    assert f'id="catManageModal{category}"'.encode() not in response.data
    response = logged_in_admin.get(f'/api/category/{category}/management')
    assert response.status_code == 200
    assert f'id="catManageModal{category}"' in response.json['html']
    assert 'data-cat-page-count="3"' in response.json['html']


def test_reordering_loaded_pages_preserves_omitted_positions(admin_user):
    ids = [db.create_page(f"Page {i}", f"stable-order-{i}", user_id=admin_user) for i in range(6)]
    db.update_pages_sort_order([ids[3], ids[1]])
    with db.get_db_context() as connection:
        actual = [row[0] for row in connection.execute('SELECT id FROM pages WHERE is_home=0 ORDER BY sort_order,title,id')]
    assert actual == [ids[0], ids[3], ids[2], ids[1], ids[4], ids[5]]


def test_sidebar_people_do_not_load_all_profiles_or_bios(client, admin_user):
    body = 'Long published profile body. ' * 10000
    for i in range(24):
        uid = db.create_user(f'person-{i:02}', 'unused-fixture-hash')
        db.upsert_user_profile(uid, bio=body, page_published=True)
    from helpers._request_cache import get_request_sidebar_people
    with client.application.test_request_context('/'):
        tracemalloc.start()
        try:
            people = get_request_sidebar_people(db.get_user_by_id(admin_user))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert len(people) == 19 and all(not person['bio'] for person in people)
    assert peak < 1024 * 1024
    assert db.list_published_profiles()[0]['bio'] == body
