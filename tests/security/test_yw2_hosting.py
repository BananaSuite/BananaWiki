"""Re-audit pass 18 regressions for the hosting portal (YW2-x)."""
from tests.hosting.conftest import login, make_account, portal, query, web  # noqa: F401
from tests.hosting.hosting_support import PASSWORD


def test_yw2_1_password_confirmations_have_a_per_account_guess_budget(web, make_account, login, query):  # noqa: F811
    from bananawiki.hosting.limits import CONFIRM_MAX_FAILURES

    victim = make_account("victim", email="")
    login(web, victim)  # holds the session, not the password
    statuses = []
    for net in range(CONFIRM_MAX_FAILURES + 5):
        statuses.append(web.post("/account", data={"username": "victim2", "email": "", "current_password": "wrong"},
                                 environ_base={"REMOTE_ADDR": f"198.51.100.{net + 1}"}).status_code)
    assert 429 not in statuses[:CONFIRM_MAX_FAILURES] and set(statuses[CONFIRM_MAX_FAILURES:]) == {429}
    assert web.post("/account/change-password", data={"current_password": PASSWORD, "new_password": "another horse 4242",
                                                      "confirm_new_password": "another horse 4242"},
                    environ_base={"REMOTE_ADDR": "198.51.100.200"}).status_code == 429
    assert query("SELECT username FROM accounts WHERE id = ?", (victim["id"],), one=True)["username"] == "victim"


def test_yw2_1_turning_two_step_off_spends_the_second_step_budget(portal, web, make_account, login, query):  # noqa: F811
    from bananawiki.core.totp import generate_secret, totp_code
    from bananawiki.hosting import mfa
    from bananawiki.hosting.db import connection_scope, db
    from bananawiki.hosting.routes.auth import MFA_ACCOUNT_LIMIT

    victim = make_account("mfavictim")
    login(web, victim)
    secret = generate_secret()
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        db.execute("UPDATE accounts SET totp_enabled = 1, totp_secret_encrypted = ?, totp_last_counter = -1 "
                   "WHERE id = ?", (mfa.encrypt_secret(secret), victim["id"]))
    statuses = [web.post("/account/mfa", data={"password": PASSWORD, "code": "000000"},
                         environ_base={"REMOTE_ADDR": f"192.0.2.{net + 1}"}).status_code
                for net in range(MFA_ACCOUNT_LIMIT + 2)]
    assert 429 in statuses
    assert web.post("/account/mfa", data={"password": PASSWORD, "code": totp_code(secret)},
                    environ_base={"REMOTE_ADDR": "192.0.2.200"}).status_code == 429
    assert query("SELECT totp_enabled FROM accounts WHERE id = ?", (victim["id"],), one=True)["totp_enabled"] == 1
