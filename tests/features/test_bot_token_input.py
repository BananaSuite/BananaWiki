"""Public account forms reject malformed proof text without application errors."""

import pytest
from conftest import PASSWORD


@pytest.mark.parametrize("token", [
    "123." + "n" * 16 + ".é",
    "١٢٣." + "n" * 16 + "." + "a" * 32,
    "123.é." + "a" * 32,
    "9" * 5000 + "." + "n" * 16 + "." + "a" * 32,
])
def test_signup_rejects_malformed_bot_token(client, db, token):
    db.execute("UPDATE site_settings SET open_signup = 1, bot_protection_enabled = 1")
    response = client.post("/signup", data={"username": "rejected", "password": PASSWORD,
                                            "confirm_password": PASSWORD, "_form_time": token})
    assert response.status_code == 400
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'rejected'") == 0
