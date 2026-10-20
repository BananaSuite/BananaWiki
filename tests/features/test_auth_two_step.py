"""Two-step verification for wiki accounts: enrolment, sign-in, recovery codes, limits and administration."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from conftest import PASSWORD, csrf_token_from

from bananawiki.core import totp
from bananawiki.wiki.features.auth import signin, two_step

from .api_support import call, enable_api, issue

DEVICE_COOKIE = "bw_session_device"
CODE_RE = re.compile(r"\b[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}\b")
TRANSLATIONS = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/auth/translations"


def _secret(client) -> str:
    page = client.get("/settings/two-step")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'src="data:image/png;base64,' in html, "the QR code is shown"
    # The key is shown in groups of four characters (easier to type); the copy button holds it whole.
    shown = re.search(r'id="two-step-key"[^>]*>([A-Z2-7 ]{32,40})<', html).group(1)
    assert re.fullmatch(r"[A-Z2-7]{4}( [A-Z2-7]{4})*", shown)
    return shown.replace(" ", "")


def _next_code(secret: str) -> str:
    """A code the wiki has not accepted yet: the next 30-second step is within the accepted window."""
    return totp.totp_code(secret, time.time() + 30)


def enrol(client, password: str = PASSWORD) -> tuple[str, list[str]]:
    secret = _secret(client)
    response = client.post("/settings/two-step", data={"action": "enable", "password": password,
                                                       "code": totp.totp_code(secret)})
    assert response.status_code == 200, response.data[:300]
    assert "no-store" in response.headers["Cache-Control"]
    codes = list(dict.fromkeys(CODE_RE.findall(response.get_data(as_text=True))))
    assert len(codes) == 10
    return secret, codes


def password_step(client, user, password: str = PASSWORD, **kwargs):
    return client.post("/login", data={"username": user["username"], "password": password}, **kwargs)


def signed_in(client) -> bool:
    return client.get("/_probe/private").data == b"private"


def audit_actions(db, user) -> list[str]:
    return [row["action"] for row in db.all("SELECT action FROM audit_log WHERE target_id = ? ORDER BY id",
                                            (user["id"],))]


# Enrolment


def test_enrolment_needs_the_password_and_a_code_and_shows_recovery_codes_once(app, client, db, make_user, login):
    user = make_user("bob")
    login(client, user)
    secret = _secret(client)
    assert client.post("/settings/two-step", data={"action": "enable", "password": "wrong password",
                                                   "code": totp.totp_code(secret)}).status_code == 302
    assert client.post("/settings/two-step", data={"action": "enable", "password": PASSWORD,
                                                   "code": "000000"}).status_code == 302
    assert not db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (user["id"],))

    response = client.post("/settings/two-step", data={"action": "enable", "password": PASSWORD,
                                                       "code": totp.totp_code(secret)})
    assert response.status_code == 200 and "no-store" in response.headers["Cache-Control"]
    codes = list(dict.fromkeys(CODE_RE.findall(response.get_data(as_text=True))))
    assert len(codes) == 10
    row = db.one("SELECT * FROM auth__two_step WHERE user_id = ?", (user["id"],))
    assert secret not in row["secret_encrypted"], "the authenticator secret is encrypted at rest"
    stored = json.loads(row["recovery_hashes"])
    assert len(stored) == 10 and not any(code in row["recovery_hashes"] or code.replace("-", "") in
                                         row["recovery_hashes"] for code in codes), "recovery codes are hashed"
    page = client.get("/settings/two-step").get_data(as_text=True)
    assert not CODE_RE.search(page) and secret not in page and secret[:4] + " " + secret[4:8] not in page, \
        "neither the codes nor the key are shown again"
    assert "user.two_step_enabled" in audit_actions(db, user)


def test_enabling_signs_out_the_other_sessions_but_keeps_this_one(app, make_user, login):
    user = make_user("bob")
    phone, laptop = app.test_client(), app.test_client()
    login(phone, user)
    login(laptop, user)
    enrol(laptop)
    assert signed_in(laptop)
    assert not signed_in(phone)


def test_the_settings_page_links_from_the_account_settings(client, make_user, login):
    login(client, make_user("bob"))
    html = client.get("/settings").get_data(as_text=True)
    assert 'href="/settings/two-step"' in html


# Sign-in


def test_sign_in_asks_for_a_code_after_the_password(app, db, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    secret, _codes = enrol(first)

    browser = app.test_client()
    response = password_step(browser, user)
    assert response.headers["Location"].endswith("/login/two-step")
    assert browser.get_cookie(DEVICE_COOKIE) is None, "the device cookie needs both steps"
    assert not signed_in(browser), "the password alone does not sign in"
    assert "id=\"code\"" in browser.get("/login/two-step").get_data(as_text=True)
    assert browser.post("/login/two-step", data={"code": "000000"}).status_code == 401
    assert not signed_in(browser)
    response = browser.post("/login/two-step", data={"code": _next_code(secret)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/")
    assert signed_in(browser)
    assert browser.get_cookie(DEVICE_COOKIE) is not None
    method = db.scalar("SELECT auth_method FROM user_sessions WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                       (user["id"],))
    assert method == "password+totp"


def test_each_authenticator_code_works_once(app, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    secret, _codes = enrol(first)
    code = _next_code(secret)
    one, two = app.test_client(), app.test_client()
    password_step(one, user)
    password_step(two, user)
    assert one.post("/login/two-step", data={"code": code}).status_code == 302
    assert two.post("/login/two-step", data={"code": code}).status_code == 401


def test_recovery_codes_sign_in_once_each(app, db, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)

    browser = app.test_client()
    password_step(browser, user)
    response = browser.post("/login/two-step", data={"code": codes[0].lower().replace("-", " ")},
                            follow_redirects=True)
    assert signed_in(browser)
    assert "Recovery codes left: 9" in response.get_data(as_text=True), "the person is told how many are left"
    assert len(json.loads(db.scalar("SELECT recovery_hashes FROM auth__two_step WHERE user_id = ?",
                                    (user["id"],)))) == 9
    assert "user.two_step_recovery_code_used" in audit_actions(db, user)

    again = app.test_client()
    password_step(again, user)
    assert again.post("/login/two-step", data={"code": codes[0]}).status_code == 401


def test_the_code_step_needs_a_current_password_step(app, db, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)

    stranger = app.test_client()
    response = stranger.post("/login/two-step", data={"code": codes[0]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert not signed_in(stranger)

    browser = app.test_client()
    password_step(browser, user)
    db.execute("UPDATE users SET password = ? WHERE id = ?", ("pbkdf2:sha256:1000$x$y", user["id"]))
    response = browser.post("/login/two-step", data={"code": codes[1]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert not signed_in(browser), "a password change in between ends the pending sign-in"
    assert len(json.loads(db.scalar("SELECT recovery_hashes FROM auth__two_step WHERE user_id = ?",
                                    (user["id"],)))) == 10, "no code was used up"


def test_the_pending_step_expires(app, make_user, login, monkeypatch):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)
    browser = app.test_client()
    password_step(browser, user)
    monkeypatch.setattr(signin, "TWO_STEP_PENDING_SECONDS", -1)
    response = browser.post("/login/two-step", data={"code": codes[0]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert not signed_in(browser)


def test_remember_me_and_the_next_page_survive_the_second_step(app, db, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)
    browser = app.test_client()
    browser.post("/login", data={"username": "bob", "password": PASSWORD, "remember_me": "1",
                                 "next": "/_probe/public-read"})
    response = browser.post("/login/two-step", data={"code": codes[0]})
    assert response.headers["Location"].endswith("/_probe/public-read")
    assert db.scalar("SELECT remember_me FROM user_sessions WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                     (user["id"],)) == 1


def test_leaving_the_second_step_forgets_the_pending_sign_in(app, db, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)
    browser = app.test_client()
    password_step(browser, user)
    page = browser.get("/login/two-step").get_data(as_text=True)
    # The way back is a form: a plain link would leave the password step passed in this browser.
    assert re.search(r'<form method="post" action="/login/two-step/cancel"[^>]*>\s*'
                     r'<input type="hidden" name="csrf_token"', page)
    assert 'href="/login"' not in page.split('<div class="card card--narrow">', 1)[1]
    assert "Back to sign-in" in page
    assert browser.get("/login/two-step/cancel").status_code == 405
    response = browser.post("/login/two-step/cancel")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    with browser.session_transaction() as state:
        assert signin.TWO_STEP_PENDING_KEY not in state
    # The code page no longer takes a code for that account, and the code is not used up.
    response = browser.post("/login/two-step", data={"code": codes[0]})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert not signed_in(browser)
    assert len(json.loads(db.scalar("SELECT recovery_hashes FROM auth__two_step WHERE user_id = ?",
                                    (user["id"],)))) == 10
    password_step(browser, user)
    assert browser.post("/login/two-step", data={"code": codes[0]}).status_code == 302
    assert signed_in(browser)


def test_leaving_the_second_step_needs_the_pages_csrf_token(app, make_user, login):
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    enrol(first)
    app.config["CSRF_DISABLED"] = False
    csrf_client = app.test_client()
    page = csrf_client.get("/login")
    csrf_client.post("/login", data={"username": "bob", "password": PASSWORD,
                                     "csrf_token": csrf_token_from(page)})
    with csrf_client.session_transaction() as state:
        assert signin.TWO_STEP_PENDING_KEY in state
    assert csrf_client.post("/login/two-step/cancel").status_code == 400
    with csrf_client.session_transaction() as state:
        assert signin.TWO_STEP_PENDING_KEY in state, "another site cannot end the pending sign-in"
    page = csrf_client.get("/login/two-step")
    response = csrf_client.post("/login/two-step/cancel", data={"csrf_token": csrf_token_from(page)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    with csrf_client.session_transaction() as state:
        assert signin.TWO_STEP_PENDING_KEY not in state


def test_administrator_sign_in_asks_for_the_code_too(app, make_user, login):
    admin = make_user("boss", role="admin")
    first = app.test_client()
    login(first, admin)
    _secret_key, codes = enrol(first)
    browser = app.test_client()
    response = browser.post("/admin/sign-in", data={"username": "boss", "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/two-step")
    assert not signed_in(browser)
    response = browser.post("/login/two-step", data={"code": codes[0]})
    assert response.headers["Location"].endswith("/admin/dashboard")


# Attempt limits


def test_strangers_with_the_password_cannot_use_up_a_known_browsers_codes(app, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_MAX_PER_DAY", 3)
    user = make_user("bob")
    owner = app.test_client()
    login(owner, user)
    _secret_key, codes = enrol(owner)
    owner.post("/logout")
    password_step(owner, user)
    assert owner.post("/login/two-step", data={"code": codes[0]}).status_code == 302
    owner.post("/logout")

    stranger = app.test_client()
    password_step(stranger, user, environ_base={"REMOTE_ADDR": "198.51.100.7"})
    for _ in range(3):
        assert stranger.post("/login/two-step", data={"code": "000000"}).status_code == 401
    assert stranger.post("/login/two-step", data={"code": codes[1]}).status_code == 429
    other_network = app.test_client()
    password_step(other_network, user, environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert other_network.post("/login/two-step", data={"code": codes[1]}).status_code == 429, \
        "browsers that never signed in share one budget per account"

    password_step(owner, user)
    assert owner.post("/login/two-step", data={"code": "000000"}).status_code == 401
    assert owner.post("/login/two-step", data={"code": codes[1]}).status_code == 302, \
        "a browser that signed in before has its own budget"
    assert signed_in(owner)


def test_codes_are_limited_per_five_minutes(app, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_MAX", 2)
    user = make_user("bob")
    first = app.test_client()
    login(first, user)
    _secret_key, codes = enrol(first)
    browser = app.test_client()
    password_step(browser, user)
    assert [browser.post("/login/two-step", data={"code": "000000"}).status_code for _ in range(3)] == [401, 401, 429]


def test_wrong_codes_in_the_settings_count_against_the_same_budget(app, db, make_user, login, monkeypatch):
    monkeypatch.setattr(signin, "TWO_STEP_MAX", 2)
    user = make_user("bob")
    client = app.test_client()
    login(client, user)
    _secret_key, codes = enrol(client)
    for _ in range(2):
        client.post("/settings/two-step", data={"action": "disable", "password": PASSWORD, "code": "000000"})
    client.post("/settings/two-step", data={"action": "disable", "password": PASSWORD, "code": codes[0]})
    assert db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (user["id"],)), \
        "a stolen session cannot guess its way past the code"


# Turning off and new codes


def test_turning_it_off_needs_the_password_and_a_code(app, db, make_user, login):
    user = make_user("bob")
    client, other = app.test_client(), app.test_client()
    login(client, user)
    _secret_key, codes = enrol(client)
    password_step(other, user)
    other.post("/login/two-step", data={"code": codes[0]})
    assert signed_in(other)

    client.post("/settings/two-step", data={"action": "disable", "password": "wrong password", "code": codes[1]})
    client.post("/settings/two-step", data={"action": "disable", "password": PASSWORD, "code": "000000"})
    assert db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (user["id"],))
    response = client.post("/settings/two-step", data={"action": "disable", "password": PASSWORD, "code": codes[1]})
    assert response.status_code == 302
    assert not db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (user["id"],))
    assert signed_in(client) and not signed_in(other)
    assert "user.two_step_disabled" in audit_actions(db, user)
    fresh = app.test_client()
    assert password_step(fresh, user).headers["Location"].endswith("/")
    assert signed_in(fresh)


def test_new_recovery_codes_replace_the_old_ones(app, db, make_user, login):
    user = make_user("bob")
    client = app.test_client()
    login(client, user)
    _secret_key, old = enrol(client)
    response = client.post("/settings/two-step", data={"action": "renew", "password": PASSWORD, "code": old[0]})
    assert response.status_code == 200 and "no-store" in response.headers["Cache-Control"]
    new = list(dict.fromkeys(CODE_RE.findall(response.get_data(as_text=True))))
    assert len(new) == 10 and not set(new) & set(old)
    assert "user.two_step_codes_renewed" in audit_actions(db, user)
    browser = app.test_client()
    password_step(browser, user)
    assert browser.post("/login/two-step", data={"code": old[1]}).status_code == 401
    assert browser.post("/login/two-step", data={"code": new[0]}).status_code == 302


def test_deleting_the_account_deletes_its_two_step_data(app, db, make_user, login):
    user = make_user("bob")
    client = app.test_client()
    login(client, user)
    enrol(client)
    client.post("/settings/delete", data={"password": PASSWORD})
    assert db.scalar("SELECT COUNT(*) FROM auth__two_step") == 0


def test_account_merges_never_move_two_step_data():
    from bananawiki.wiki.features.user_data_export.service import EXPORTED
    from bananawiki.wiki.features.users.merge import KEEP_WITH_SOURCE

    assert "auth__two_step" in KEEP_WITH_SOURCE, "a merge must not give the source's authenticator to the target"
    # The data export says whether it is on, never the key (test_data_export checks the archive).
    exported = next(spec.sql for spec in EXPORTED if spec.table == "auth__two_step")
    assert "secret_encrypted" not in exported and "last_counter" not in exported


# Required for administrators


def _settings_form(**overrides):
    form = {
        "site_name": "BananaWiki", "timezone": "UTC", "approval_pending_timeout_hours": "0",
        "approval_denied_timeout_hours": "24", "maintenance_message": "", "public_mode_message": "",
        "upload_mode": "allow_all", "upload_whitelist": "", "upload_blacklist": "", "upload_max_size_mb": "50",
        "upload_quota_per_day_count": "0", "upload_quota_per_day_bytes": "0", "draft_expiration_hours": "0",
        "intro_role_switching_roles": ["user", "editor", "admin"],
    }
    form.update(overrides)
    return form


def test_only_owners_and_superusers_can_require_it_of_administrators(app, db, make_user, login):
    plain = app.test_client()
    login(plain, make_user("admin1", role="admin"))
    assert plain.post("/admin/settings", data=_settings_form(two_step_required_for_admins="1")).status_code == 302
    assert db.scalar("SELECT two_step_required_for_admins FROM site_settings") == 0
    assert "disabled" in plain.get("/admin/settings").get_data(as_text=True).split(
        'name="two_step_required_for_admins"', 1)[1].split(">", 1)[0]

    owner = app.test_client()
    login(owner, make_user("boss", role="owner"))
    assert owner.post("/admin/settings", data=_settings_form(two_step_required_for_admins="1")).status_code == 302
    assert db.scalar("SELECT two_step_required_for_admins FROM site_settings") == 0, \
        "owners turn it on for themselves first, so requiring it cannot lock them out"
    assert "Turn on two-step verification for your own account first" in owner.get("/admin/settings").get_data(
        as_text=True)
    enrol(owner)
    assert owner.post("/admin/settings", data=_settings_form(two_step_required_for_admins="1")).status_code == 302
    assert db.scalar("SELECT two_step_required_for_admins FROM site_settings") == 1
    assert plain.post("/admin/settings", data=_settings_form()).status_code in (302, 403)
    assert db.scalar("SELECT two_step_required_for_admins FROM site_settings") == 1, \
        "a plain administrator's form leaves it as it is"


def test_required_two_step_holds_administrators_until_they_turn_it_on(app, db, make_user, login):
    db.execute("UPDATE site_settings SET two_step_required_for_admins = 1")
    admin = make_user("admin1", role="admin")
    member = make_user("member")
    client = app.test_client()
    response = password_step(client, admin)
    assert response.headers["Location"].endswith("/settings/two-step")
    response = client.get("/_probe/private")
    assert response.status_code == 302 and response.headers["Location"].endswith("/settings/two-step")
    blocked = client.get("/_probe/private", headers={"Accept": "application/json"})
    assert blocked.status_code == 403 and blocked.get_json()["code"] == "account_step_required"
    page = client.get("/settings/two-step").get_data(as_text=True)
    assert "must" in page.lower() and 'action="/logout"' in page
    enrol(client)
    assert signed_in(client)

    others = app.test_client()
    login(others, member)
    assert signed_in(others), "members are not affected"


def test_api_tokens_keep_working_while_it_is_required(app, client, db, make_user):
    enable_api(app, db)
    admin = make_user("admin1", role="admin")
    token = issue(app, admin)
    db.execute("UPDATE site_settings SET two_step_required_for_admins = 1")
    assert call(client, "GET", "/pages", token).status_code == 200


def test_impersonation_skips_the_second_step_and_cannot_change_it(app, db, make_user, login):
    boss = make_user("boss", role="owner", is_superuser=1)
    target = make_user("admin1", role="admin")
    target_client = app.test_client()
    login(target_client, target)
    enrol(target_client)
    db.execute("UPDATE site_settings SET two_step_required_for_admins = 1")
    other_admin = make_user("admin2", role="admin")

    client = app.test_client()
    login(client, boss)
    enrol(client)
    for person in (target, other_admin):
        assert client.post(f"/admin/users/{person['id']}/impersonate").status_code == 302
        assert signed_in(client), "viewing as someone needs no code and no set-up"
        assert client.post("/settings/two-step", data={"action": "disable", "password": PASSWORD,
                                                       "code": "000000"}).status_code == 403
        client.post("/impersonation/stop")
    assert db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (target["id"],))


# Reset by an owner or superuser


def test_an_owner_resets_a_locked_out_administrators_two_step(app, db, make_user, login):
    owner = make_user("boss", role="owner")
    admin = make_user("admin1", role="admin")
    admin_client = app.test_client()
    login(admin_client, admin)
    enrol(admin_client)

    client = app.test_client()
    login(client, owner)
    detail = client.get(f"/admin/users/{admin['id']}").get_data(as_text=True)
    assert f'action="/admin/users/{admin["id"]}/two-step/reset"' in detail
    response = client.post(f"/admin/users/{admin['id']}/two-step/reset")
    assert response.status_code == 302
    assert not db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (admin["id"],))
    assert not signed_in(admin_client), "every session of the account ends"
    entry = db.one("SELECT * FROM audit_log WHERE action = 'user.two_step_reset'")
    assert entry["actor_id"] == owner["id"] and entry["target_id"] == admin["id"]
    fresh = app.test_client()
    assert password_step(fresh, admin).headers["Location"].endswith("/")


def test_plain_administrators_cannot_reset_two_step(app, db, make_user, login):
    admin = make_user("admin1", role="admin")
    member = make_user("member")
    member_client = app.test_client()
    login(member_client, member)
    enrol(member_client)
    client = app.test_client()
    login(client, admin)
    detail = client.get(f"/admin/users/{member['id']}").get_data(as_text=True)
    assert "/two-step/reset" not in detail
    client.post(f"/admin/users/{member['id']}/two-step/reset")
    assert db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (member["id"],))
    assert not db.scalar("SELECT 1 FROM audit_log WHERE action = 'user.two_step_reset'")


@pytest.mark.parametrize("kind", ["self", "superuser", "other_owner"])
def test_resets_follow_the_account_hierarchy(app, db, make_user, login, kind):
    owner = make_user("boss", role="owner")
    if kind == "self":
        target = owner
    else:
        target = make_user("chief", role="owner", is_superuser=1 if kind == "superuser" else 0)
    target_client = app.test_client()
    login(target_client, target)
    enrol(target_client)
    if kind == "self":
        client = target_client
    else:
        client = app.test_client()
        login(client, owner)
    client.post(f"/admin/users/{target['id']}/two-step/reset")
    assert db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (target["id"],))
    assert not db.scalar("SELECT 1 FROM audit_log WHERE action = 'user.two_step_reset'")


def test_reset_command_turns_it_off_from_the_server(app, db, make_user, login, capsys, monkeypatch):
    from bananawiki import cli

    user = make_user("bob", role="owner")
    client = app.test_client()
    login(client, user)
    enrol(client)
    environ = {"BW_ENV": "test", "BW_INSTANCE_DIR": app.config["BW"].instance_dir, "BW_SETUP_TOKEN": "tok-123",
               "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000", "SECRET_KEY": app.config["SECRET_KEY"],
               "BW_BACKGROUND_JOBS": "0"}
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BW_DATABASE_PATH", raising=False)
    assert cli.main(["reset-two-step", "bob"]) == 0
    assert json.loads(capsys.readouterr().out) == {"reset": "bob", "sessions_revoked": True}
    assert not db.scalar("SELECT 1 FROM auth__two_step WHERE user_id = ?", (user["id"],))
    assert not signed_in(client)
    assert "user.two_step_reset" in audit_actions(db, user)
    assert cli.main(["reset-two-step", "bob"]) == 1, "nothing to reset"
    assert cli.main(["reset-two-step", "nobody"]) == 1


def test_the_installed_command_passes_the_reset_to_the_application():
    """``sudo bananawiki reset-two-step NAME`` on a managed server runs it as the service account."""
    from bananawiki.ops import cli as ops_cli

    args = ops_cli.parser().parse_args(["--root", "/opt/bananawiki", "reset-two-step", "alice"])
    assert args.command == "reset-two-step" and args.arguments == ["alice"]


# Catalogues


def test_translations_have_the_same_keys_and_placeholders():
    english = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    italian = json.loads((TRANSLATIONS / "it.json").read_text(encoding="utf-8"))
    assert set(english) == set(italian)
    for key, text in english.items():
        assert set(re.findall(r"{(\w+)}", text)) == set(re.findall(r"{(\w+)}", italian[key])), key


def test_hosting_and_wiki_keys_never_match():
    with_hosting = totp.recovery_hash("same secret key", "ABCD-EF01-2345", "mfa-recovery-v1")
    with_wiki = totp.recovery_hash("same secret key", "ABCD-EF01-2345", two_step._RECOVERY_LABEL)
    assert with_hosting != with_wiki
