import db


def _search_results_html(html):
    start = html.find('<div class="advanced-search-page">')
    assert start >= 0
    return html[start:]


def test_advanced_search_finds_content_and_highlights(logged_in_admin, admin_user):
    db.create_page(
        "Release Notes",
        "release-notes",
        "The launch checklist includes a migration window and rollback notes.",
        user_id=admin_user,
    )

    resp = logged_in_admin.get("/search?q=migration")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Release Notes" in html
    assert "<mark>migration</mark>" in html
    assert "launch checklist" in html


def test_advanced_search_supports_field_filters_and_negative_terms(logged_in_admin, admin_user):
    db.create_page(
        "Operations Runbook",
        "operations-runbook",
        "Database backup schedule and restore plan.",
        user_id=admin_user,
    )
    db.create_page(
        "Operations Draft",
        "operations-draft",
        "Database backup draft.",
        user_id=admin_user,
    )

    resp = logged_in_admin.get("/search", query_string={"q": "title:Operations content:backup -Draft"})
    assert resp.status_code == 200
    html = _search_results_html(resp.get_data(as_text=True))
    assert 'href="/page/operations-runbook"' in html
    assert "<mark>Operations</mark> Runbook" in html
    assert "Operations Draft" not in html


def test_advanced_search_empty_query_opens_page_without_listing_everything(logged_in_admin, admin_user):
    db.create_page("Hidden Until Query", "hidden-until-query", "needle", user_id=admin_user)

    resp = logged_in_admin.get("/search")
    assert resp.status_code == 200
    html = _search_results_html(resp.get_data(as_text=True))
    assert "Type a query above" in html
    assert "Hidden Until Query" not in html


def test_advanced_search_includes_categories(logged_in_admin):
    db.create_category("Engineering Notes")

    resp = logged_in_admin.get("/search?q=Engineering&type=category")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Engineering Notes" in html
    assert "Categories" in html


def test_sidebar_enter_navigates_to_advanced_search():
    with open("app/static/js/sidebar-search.js", encoding="utf-8") as fh:
        script = fh.read()
    assert "e.key === 'Enter'" in script
    assert "window.location.href = '/search'" in script
