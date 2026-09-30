"""Portal hardening: session cookie scope, login limits, reset tokens, wiki names, proxy capacity."""

from __future__ import annotations

from bananawiki.hosting import wikihosts

from .hosting_support import PASSWORD


def test_https_sessions_use_a_host_only_cookie_that_wikis_cannot_plant(web, make_account):
    account = make_account()
    response = web.post("https://localhost/login", data={"username": account["username"], "password": PASSWORD})
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("__Host-bwh_session=") and "Secure" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie
    assert web.get("https://localhost/dashboard").status_code == 200
    # A same-named cookie without the prefix (what a sibling wiki could set) is not a session.
    other = web.application.test_client()
    other.set_cookie("bwh_session", web.get_cookie("__Host-bwh_session", domain="localhost").value)
    assert other.get("https://localhost/dashboard").status_code == 302


def test_a_successful_sign_in_does_not_reset_the_per_ip_limit(web, make_account, query):
    good = make_account()
    for _ in range(3):
        web.post("/login", data={"username": "victim", "password": "wrong"})
    web.post("/login", data={"username": good["username"], "password": PASSWORD})
    rows = query("SELECT COUNT(*) AS n FROM hosting_rate_limit_hits WHERE bucket = 'login-failed'", one=True)
    assert rows["n"] == 3


def test_changing_the_email_address_voids_a_pending_reset_link(ctx, make_account, query):
    from bananawiki.hosting import accounts

    account = make_account(email="old@example.com")
    query("UPDATE accounts SET password_reset_token_hash = 'x', password_reset_expires_at = '2999-01-01 00:00:00' "
          "WHERE id = ?", (account["id"],))
    accounts.update_identity(accounts.get(account["id"]), username=account["username"], email="new@example.com",
                             email_required=False)
    row = query("SELECT password_reset_token_hash AS h FROM accounts WHERE id = ?", (account["id"],), one=True)
    assert row["h"] == ""


def test_an_apex_name_cannot_take_a_hosting_wikis_address(ctx, make_account, make_wiki):
    import pytest

    from bananawiki.hosting import instances
    from bananawiki.hosting.errors import ServiceError

    make_wiki(make_account(), "team")
    admin = make_account(admin=True)
    with pytest.raises(ServiceError):
        instances.create(admin, "team-hosting", domain_mode="apex")


def test_proxied_requests_never_take_every_portal_thread():
    slots = wikihosts._Slots(total=2)
    first, second, third = slots.get("a"), slots.get("b"), slots.get("c")
    assert first.acquire(0.01) and second.acquire(0.01)
    assert not third.acquire(0.01)
    first.release()
    assert third.acquire(0.01)


def test_hosted_wikis_never_receive_the_gpu_master_token(ctx, make_account, make_wiki):
    from bananawiki.hosting import instances, settings

    master = "master-token-" + "x" * 20
    settings.update(global_tts_gpu_enabled=1, global_tts_gpu_url="https://gpu.example.org",
                    global_tts_gpu_auth_token=master)
    inst = make_wiki(make_account(), "team")
    gpu = instances.policy(instances.get(inst["id"])).tts_gpu
    assert gpu is not None and gpu.token != master
    assert gpu.token == instances.tenant_tts_token(master, inst["id"])
