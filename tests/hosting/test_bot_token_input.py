"""Malformed public sign-in proofs must not crash the hosting portal."""

import pytest

from .hosting_support import PASSWORD, build_portal


@pytest.mark.parametrize("token", ["١.invalid", "123.é", "9" * 5000 + "." + "a" * 20])
def test_signin_rejects_malformed_bot_token(tmp_path, token):
    portal = build_portal(tmp_path, bot_protection=True)
    response = portal.test_client().post("/login", data={"username": "rejected", "password": PASSWORD,
                                                       "_form_time": token})
    assert response.status_code == 400
