def test_site_about_route_removed(logged_in_admin):
    response = logged_in_admin.get("/site")

    assert response.status_code == 404
