"""Health endpoint tests."""


def test_health_returns_200(client):
    """GET /health returns 200 when app is running normally."""
    res = client.get("/health")
    assert res.status_code == 200


def test_health_returns_ok_json(client):
    """GET /health returns a JSON ok payload."""
    res = client.get("/health")
    assert res.is_json
    assert res.get_json() == {"status": "ok"}


def test_health_is_public_without_auth(client):
    """GET /health does not redirect to login when anonymous."""
    res = client.get("/health", follow_redirects=False)
    assert res.status_code == 200
    assert res.headers.get("Location") is None


def test_health_is_accessible_without_csrf_token(client):
    """GET /health works without CSRF token headers/cookies."""
    res = client.get("/health")
    assert res.status_code == 200
