"""How members sign in to a hosted wiki: wiki accounts only, hosting accounts only, or both.

Covers the three modes, the host's ``BW_PLATFORM_SIGNIN_MODE`` (which wins
and locks the setting), the lock-out guard, the administrators' recovery
page, the ``signin-mode`` command and the protections of the first platform
sign-in and of account merges while only hosting accounts may sign in.
"""

from __future__ import annotations

import json
import logging

import pytest
from conftest import PASSWORD, make_config

from bananawiki.core.ratelimit import SqlLimiter
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import platform_oauth, routes_recovery, signin_modes
from tests.features.pages_support import set_feature
from tests.features.test_auth_oauth import OAUTH_ENV, FakePortal, _callback, _start
from tests.features.test_auth_two_step import _next_code, enrol, signed_in

LINKED_AT = "2026-01-01 00:00:00"


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setattr(platform_oauth.http, "request", fake.request)
    monkeypatch.setattr(platform_oauth, "portal_is_private", lambda url: False)
    return fake


@pytest.fixture
def build(app_factory):
    """A wiki with platform sign-in configured; ``mode`` is the host's BW_PLATFORM_SIGNIN_MODE."""

    def make(mode: str | None = None, *, stored: str | None = None, configured: bool = True):
        environ = dict(OAUTH_ENV) if configured else {}
        if mode is not None:
            environ["BW_PLATFORM_SIGNIN_MODE"] = mode
        wiki = app_factory(environ=environ)
        if stored is not None:
            database(wiki).execute("UPDATE site_settings SET signin_mode = ?", (stored,))
        return wiki

    return make


def database(wiki):
    from bananawiki.core.sqlite import Session

    return Session(wiki.extensions["bananawiki.database"].connect())


def user_in(wiki, username, *, role="user", password=PASSWORD):
    from bananawiki.wiki import accounts

    with wiki.test_request_context(), connection_scope():
        return accounts.create(username, password, role=role, emit_event=False)


def link(wiki, portal, user, account_id):
    database(wiki).execute("INSERT INTO platform_oauth_links (account_id, user_id, portal_username, linked_at) "
                           "VALUES (?, ?, ?, ?)", (account_id, user["id"], user["username"], LINKED_AT))
    portal.links[user["id"]] = account_id


def mode_of(wiki):
    with wiki.test_request_context(), connection_scope():
        return signin_modes.current()


def password_sign_in(client, user, password=PASSWORD, path="/login"):
    return client.post(path, data={"username": user["username"], "password": password})


def platform_sign_in(client, portal, sub, username):
    portal.profile = {"sub": sub, "username": username}
    return _callback(client, _start(client))


def audit_rows(wiki, action):
    return database(wiki).all("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))


PLATFORM_ROUTES = ("/platform-oauth/login", "/platform-oauth/callback?code=x&state=y", "/platform-oauth/connect",
                   "/platform-oauth/signup", "/platform-oauth/merge")


# Configuration


def test_the_host_variable_is_read_and_unknown_values_are_ignored_with_a_warning(tmp_path, caplog):
    for raw, expected in (("platform", "platform"), (" Local ", "local"), ("both", "both"), ("", "")):
        assert make_config(tmp_path, environ={"BW_PLATFORM_SIGNIN_MODE": raw}).platform_signin_mode == expected
    assert make_config(tmp_path).platform_signin_mode == ""
    with caplog.at_level(logging.WARNING, logger="bananawiki.config"):
        assert make_config(tmp_path, environ={"BW_PLATFORM_SIGNIN_MODE": "hosting"}).platform_signin_mode == ""
    assert "BW_PLATFORM_SIGNIN_MODE" in caplog.text


def test_new_wikis_default_to_both(build):
    wiki = build()
    assert database(wiki).scalar("SELECT signin_mode FROM site_settings") == "both"
    assert mode_of(wiki) == "both"


def test_without_platform_sign_in_the_mode_is_always_local(build):
    wiki = build("platform", stored="platform", configured=False)
    assert mode_of(wiki) == "local"
    client = wiki.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert 'name="password"' in page and "/platform-oauth/login" not in page
    assert password_sign_in(client, user_in(wiki, "bob")).status_code == 302
    assert client.get("/platform-oauth/login").status_code == 404
    assert client.get("/login/recovery").status_code == 302


# local


def test_local_mode_hides_the_portal_and_closes_its_routes(build, portal):
    wiki = build(stored="local")
    bob = user_in(wiki, "bob")
    link(wiki, portal, bob, "acct-bob")
    client = wiki.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert 'name="password"' in page and "/platform-oauth/login" not in page
    for path in PLATFORM_ROUTES:
        assert client.get(path).status_code == 404, path
    assert client.post("/platform-oauth/merge", data={"username": "bob", "password": PASSWORD}).status_code == 404
    assert password_sign_in(client, bob).status_code == 302
    assert client.get("/settings/link-platform-account").status_code == 404
    assert client.get("/platform-oauth/link-callback?state=x").status_code == 404
    assert client.post("/settings/unlink-platform-account", data={"password": PASSWORD}).status_code == 404
    assert "/settings/link-platform-account" not in client.get("/settings/security").get_data(as_text=True)
    # The link made earlier stays, unused.
    assert database(wiki).scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-bob'") == bob["id"]


# both


def test_both_mode_offers_the_password_form_and_the_portal(build, portal):
    wiki = build()
    bob = user_in(wiki, "bob")
    link(wiki, portal, bob, "acct-bob")
    page = wiki.test_client().get("/login").get_data(as_text=True)
    assert 'name="password"' in page and "/platform-oauth/login" in page
    assert password_sign_in(wiki.test_client(), bob).status_code == 302
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-bob", "bob")
    assert signed_in(client)


# platform


def test_platform_mode_shows_only_the_portal(build):
    wiki = build(stored="platform")
    database(wiki).execute("UPDATE site_settings SET open_signup = 1")
    page = wiki.test_client().get("/login?next=/page/home").get_data(as_text=True)
    assert 'name="password"' not in page and 'href="/signup"' not in page
    assert 'class="btn btn--primary" href="/platform-oauth/login?next=/page/home"' in page
    assert "Your account here is created when you first sign in" in page
    assert "/login/recovery" not in page, "the recovery page is not advertised"


def test_platform_mode_refuses_passwords_everywhere_but_recovery(build):
    wiki = build(stored="platform")
    bob = user_in(wiki, "bob")
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    refused = password_sign_in(client, bob)
    assert refused.status_code == 403
    assert "only accepts sign-in with the hosting portal" in refused.get_data(as_text=True)
    assert not signed_in(client)
    assert client.get("/admin/sign-in").headers["Location"].endswith("/login")
    assert password_sign_in(client, chief, path="/admin/sign-in").status_code == 403
    assert password_sign_in(client, chief, path="/admin").status_code == 403, "the 1.4 form target"
    assert password_sign_in(client, bob, path="/session-conflict/force").status_code == 403
    assert not signed_in(client)
    assert database(wiki).scalar("SELECT COUNT(*) FROM user_sessions") == 0


def test_platform_mode_session_conflict_page_offers_the_portal(build, portal):
    wiki = build(stored="platform")
    database(wiki).execute("UPDATE site_settings SET session_limit_enabled = 1")
    bob = user_in(wiki, "bob")
    link(wiki, portal, bob, "acct-bob")
    first, second = wiki.test_client(), wiki.test_client()
    platform_sign_in(first, portal, "acct-bob", "bob")
    platform_sign_in(second, portal, "acct-bob", "bob")
    response = first.get("/_probe/private")
    assert response.headers["Location"].split("?")[0].endswith("/session-conflict")
    page = first.get(response.headers["Location"]).get_data(as_text=True)
    assert 'name="password"' not in page and "/platform-oauth/login" in page


def test_platform_mode_sends_sign_up_through_the_portal_with_the_invite(build, portal):
    wiki = build(stored="platform")
    db = database(wiki)
    chief = user_in(wiki, "chief", role="admin")
    db.insert("invite_codes", {"code": "ABCD-1234", "created_by": chief["id"], "max_uses": 1})
    client = wiki.test_client()
    assert client.get("/signup").headers["Location"].endswith("/platform-oauth/login")
    refused = client.post("/signup", data={"username": "newbie", "password": "x" * 20, "confirm_password": "x" * 20})
    assert refused.headers["Location"].endswith("/platform-oauth/login")
    assert db.scalar("SELECT COUNT(*) FROM users WHERE username = 'newbie'") == 0
    assert client.get("/signup?code=abcd-1234").headers["Location"].endswith("/platform-oauth/login")
    response = platform_sign_in(client, portal, "acct-new", "newbie")
    assert response.headers["Location"].endswith("/platform-oauth/connect")
    page = client.get("/platform-oauth/connect").get_data(as_text=True)
    assert 'value="ABCD-1234"' in page, "the invite code of the link is offered"
    created = client.post("/platform-oauth/signup", data={"username": "newbie", "invite_code": "ABCD-1234"})
    assert created.status_code == 302
    newbie = db.one("SELECT * FROM users WHERE username = 'newbie'")
    assert db.scalar("SELECT account_id FROM platform_oauth_links WHERE user_id = ?", (newbie["id"],)) == "acct-new"
    assert signed_in(client)


def test_platform_mode_new_accounts_follow_the_sign_up_policy(build, portal):
    wiki = build(stored="platform")
    db = database(wiki)
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-new", "newbie")
    refused = client.post("/platform-oauth/signup", data={"username": "newbie"})
    assert refused.status_code == 400, "closed sign-up needs an invite"
    db.execute("UPDATE site_settings SET open_signup = 1, approval_required = 1")
    created = client.post("/platform-oauth/signup", data={"username": "newbie"})
    assert created.headers["Location"].endswith("/account-status")
    assert db.scalar("SELECT approval_status FROM users WHERE username = 'newbie'") == "pending"


def test_platform_mode_members_connect_their_existing_account_with_password_and_code(build, portal):
    wiki = build("both")
    bob = user_in(wiki, "bob")
    client = wiki.test_client()
    password_sign_in(client, bob)
    secret, _codes = enrol(client)
    wiki = build("platform")  # the host switches the same wiki to hosting accounts only
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-bob", "bobby")
    response = client.post("/platform-oauth/merge", data={"username": "bob", "password": PASSWORD})
    assert response.headers["Location"].endswith("/platform-oauth/merge"), "the code comes next"
    assert database(wiki).scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    client.post("/platform-oauth/merge", data={"code": _next_code(secret)})
    assert database(wiki).scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-bob'") \
        == bob["id"]
    assert signed_in(client)


def test_platform_mode_leaves_api_tokens_alone(build):
    from tests.features.api_support import call, enable_api, issue

    wiki = build(stored="platform")
    enable_api(wiki, database(wiki))
    bob = user_in(wiki, "bob")
    database(wiki).execute("UPDATE users SET api_access_enabled = 1 WHERE id = ?", (bob["id"],))
    token = issue(wiki, bob)
    assert call(wiki.test_client(), "GET", "/pages", token).status_code == 200


# The host's choice


def test_the_host_overrides_the_wiki_and_locks_the_setting(build, portal):
    wiki = build("platform", stored="both")
    assert mode_of(wiki) == "platform"
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    client.post("/login/recovery", data={"username": "chief", "password": PASSWORD})
    page = client.get("/admin/settings?section=security").get_data(as_text=True)
    assert '<select id="f-signin_mode" name="signin_mode" disabled' in page
    assert '<option value="platform" selected>' in page
    assert "Set by your hosting provider." in page
    response = client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "local"})
    assert response.status_code == 302
    assert database(wiki).scalar("SELECT signin_mode FROM site_settings") == "both"
    assert mode_of(wiki) == "platform"
    assert chief


def test_the_host_can_also_reopen_passwords(build):
    wiki = build("local", stored="platform")
    assert mode_of(wiki) == "local"
    assert 'name="password"' in wiki.test_client().get("/login").get_data(as_text=True)
    assert mode_of(build("both", stored="platform")) == "both"


# The administrators' choice and the lock-out guard


def test_administrators_choose_the_mode(build, portal):
    wiki = build()
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    password_sign_in(client, chief)
    page = client.get("/admin/settings?section=security").get_data(as_text=True)
    assert '<select id="f-signin_mode" name="signin_mode"' in page and "disabled" not in page.split("f-signin_mode")[1][:60]
    assert "/login/recovery" in page, "the help text names the recovery page"
    assert client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "local"}) \
        .status_code == 302
    assert mode_of(wiki) == "local"
    assert audit_rows(wiki, "settings.updated")[-1]["details"].find("signin_mode") >= 0
    refused = client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "nonsense"})
    assert refused.status_code == 400 and mode_of(wiki) == "local"


def test_switching_to_hosting_only_needs_your_own_link(build, portal):
    wiki = build()
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    password_sign_in(client, chief)
    refused = client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "platform"})
    assert refused.status_code == 400
    assert "Connect your own account to BananaWiki Hosting first" in refused.get_data(as_text=True)
    assert database(wiki).scalar("SELECT signin_mode FROM site_settings") == "both"
    link(wiki, portal, chief, "acct-chief")
    assert client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "platform"}) \
        .status_code == 302
    assert mode_of(wiki) == "platform"
    # Saving the tab again once it is on needs nothing more.
    database(wiki).execute("DELETE FROM platform_oauth_links")
    assert client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "platform"}) \
        .status_code == 302


def test_the_setting_is_hidden_without_platform_sign_in_and_old_forms_keep_it(build, portal):
    unconfigured = build(configured=False)
    chief = user_in(unconfigured, "chief", role="admin")
    client = unconfigured.test_client()
    password_sign_in(client, chief)
    assert "signin_mode" not in client.get("/admin/settings?section=security").get_data(as_text=True)
    client.post("/admin/settings?section=security", data={"section": "security", "signin_mode": "platform"})
    assert database(unconfigured).scalar("SELECT signin_mode FROM site_settings") == "both"


def test_the_api_cannot_change_the_mode(build):
    from bananawiki.wiki.features.api_service import settings_rules

    assert "signin_mode" not in settings_rules.RULES


def test_the_guard_function(build, portal):
    wiki = build()
    chief = user_in(wiki, "chief", role="admin")
    with wiki.test_request_context(), connection_scope():
        assert signin_modes.platform_refusal(chief) == "auth.signin_mode.error.link_yours_first"
        assert signin_modes.platform_refusal(None) == "auth.signin_mode.error.link_yours_first"
    link(wiki, portal, chief, "acct-chief")
    with wiki.test_request_context(), connection_scope():
        assert signin_modes.platform_refusal(chief) is None
    unconfigured = build(configured=False)
    with unconfigured.test_request_context(), connection_scope():
        assert signin_modes.platform_refusal(chief) == "auth.signin_mode.error.not_configured"


# Recovery


def test_recovery_sends_to_the_normal_sign_in_in_other_modes(build):
    for stored in ("both", "local"):
        response = build(stored=stored).test_client().get("/login/recovery?next=/page/home")
        assert response.status_code == 302 and "/login?next=" in response.headers["Location"]


def test_recovery_signs_administrators_in_and_is_audited(build):
    wiki = build(stored="platform")
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    page = client.get("/login/recovery")
    assert page.status_code == 200 and 'name="password"' in page.get_data(as_text=True)
    response = password_sign_in(client, chief, path="/login/recovery")
    assert response.status_code == 302
    assert signed_in(client)
    [row] = audit_rows(wiki, "user.recovery_sign_in")
    assert row["actor_id"] == chief["id"] and row["target_id"] == chief["id"]
    assert json.loads(row["details"]) == {"username": "chief", "two_step": False}
    method = database(wiki).scalar("SELECT auth_method FROM user_sessions WHERE user_id = ?", (chief["id"],))
    assert method == "recovery"


def test_recovery_owners_too_and_two_step_is_still_asked(build):
    wiki = build("both")
    owner = user_in(wiki, "boss", role="owner")
    client = wiki.test_client()
    password_sign_in(client, owner)
    secret, _codes = enrol(client)
    wiki = build("platform")
    client = wiki.test_client()
    response = password_sign_in(client, owner, path="/login/recovery")
    assert response.headers["Location"].endswith("/login/two-step")
    assert not signed_in(client)
    client.post("/login/two-step", data={"code": _next_code(secret)})
    assert signed_in(client)
    assert json.loads(audit_rows(wiki, "user.recovery_sign_in")[-1]["details"])["two_step"] is True


def test_recovery_refuses_members_like_a_wrong_password(build):
    wiki = build(stored="platform")
    bob = user_in(wiki, "bob")
    chief = user_in(wiki, "chief", role="admin")
    member = password_sign_in(wiki.test_client(), bob, path="/login/recovery")
    wrong = password_sign_in(wiki.test_client(), chief, password="not the password", path="/login/recovery")
    nobody = wiki.test_client().post("/login/recovery", data={"username": "ghost", "password": PASSWORD})
    for response in (member, wrong, nobody):
        assert response.status_code == 401
        assert "Wrong username or password." in response.get_data(as_text=True)
    assert database(wiki).scalar("SELECT COUNT(*) FROM user_sessions") == 0
    reasons = [(row["target_id"], json.loads(row["details"])["reason"])
               for row in audit_rows(wiki, "user.recovery_sign_in_failed")]
    assert reasons == [(bob["id"], "not_admin"), (chief["id"], "wrong_password"), (None, "unknown_account")]
    assert all(row["actor_id"] is None for row in audit_rows(wiki, "user.recovery_sign_in_failed"))
    assert "ghost" not in json.dumps(database(wiki).all("SELECT details FROM audit_log"))


def test_recovery_counts_a_member_attempt_as_a_failure(build):
    wiki = build(stored="platform")
    bob = user_in(wiki, "bob")
    with wiki.test_request_context(), connection_scope() as session:
        before = SqlLimiter(session).count(f"id:{bob['id']}", "login:account", 900)
    password_sign_in(wiki.test_client(), bob, path="/login/recovery")
    with wiki.test_request_context(), connection_scope() as session:
        assert SqlLimiter(session).count(f"id:{bob['id']}", "login:account", 900) == before + 1
        assert SqlLimiter(session).count(f"id:{bob['id']}", "login:ok-account", 900) == 0


def test_recovery_is_rate_limited(build):
    wiki = build(stored="platform")
    chief = user_in(wiki, "chief", role="admin")
    client = wiki.test_client()
    for number in range(routes_recovery.MAX_PER_IP):
        response = client.post("/login/recovery", data={"username": f"guess{number}", "password": "nope nope"})
        assert response.status_code == 401
    blocked = password_sign_in(client, chief, path="/login/recovery")
    assert blocked.status_code == 429 and not signed_in(client)
    last = audit_rows(wiki, "user.recovery_sign_in_failed")[-1]
    assert json.loads(last["details"])["reason"] == "too_many_attempts" and last["target_id"] == chief["id"]


def test_recovery_is_audited_even_while_the_audit_log_is_off(build):
    wiki = build(stored="platform")
    chief = user_in(wiki, "chief", role="admin")
    set_feature(wiki, "audit", False)
    assert database(wiki).scalar("SELECT enabled FROM plugins WHERE id = 'audit'") == 0
    client = wiki.test_client()
    password_sign_in(client, chief, path="/login/recovery")
    assert len(audit_rows(wiki, "user.recovery_sign_in")) == 1
    assert audit_rows(wiki, "user.login") == [], "the audit log is off for everything else"


# The command line


def _cli_environ(wiki, monkeypatch, **extra):
    environ = {"BW_ENV": "test", "BW_INSTANCE_DIR": wiki.config["BW"].instance_dir, "BW_SETUP_TOKEN": "tok-123",
               "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000", "SECRET_KEY": wiki.config["SECRET_KEY"],
               "BW_BACKGROUND_JOBS": "0", **OAUTH_ENV, **extra}
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BW_DATABASE_PATH", raising=False)
    if "BW_PLATFORM_SIGNIN_MODE" not in extra:
        monkeypatch.delenv("BW_PLATFORM_SIGNIN_MODE", raising=False)


def test_the_command_shows_and_resets_the_mode(build, monkeypatch, capsys):
    from bananawiki import cli

    wiki = build(stored="platform")
    _cli_environ(wiki, monkeypatch)
    assert cli.main(["signin-mode"]) == 0
    assert json.loads(capsys.readouterr().out) == {"stored": "platform", "effective": "platform",
                                                   "host_override": None, "platform_sign_in_configured": True}
    assert cli.main(["signin-mode", "both"]) == 0
    assert json.loads(capsys.readouterr().out)["effective"] == "both"
    assert database(wiki).scalar("SELECT signin_mode FROM site_settings") == "both"
    [row] = audit_rows(wiki, "settings.updated")
    assert json.loads(row["details"]) == {"changed": "signin_mode", "via": "command line"}
    assert 'name="password"' in wiki.test_client().get("/login").get_data(as_text=True)
    with pytest.raises(SystemExit):
        cli.main(["signin-mode", "everyone"])


def test_the_command_says_when_the_host_decides(build, monkeypatch, capsys):
    from bananawiki import cli

    wiki = build(stored="both")
    _cli_environ(wiki, monkeypatch, BW_PLATFORM_SIGNIN_MODE="platform")
    assert cli.main(["signin-mode", "local"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"stored": "local", "effective": "platform", "host_override": "platform",
                                        "platform_sign_in_configured": True}
    assert "BW_PLATFORM_SIGNIN_MODE" in captured.err


def test_the_installed_command_passes_it_to_the_application():
    from bananawiki.ops import cli as ops_cli

    args = ops_cli.parser().parse_args(["--root", "/opt/bananawiki", "signin-mode", "both"])
    assert args.command == "signin-mode" and args.arguments == ["both"]


# Protections while only hosting accounts may sign in


def test_a_same_named_account_is_never_linked_by_name(build, portal):
    wiki = build(stored="platform")
    alex = user_in(wiki, "alex")
    client = wiki.test_client()
    response = platform_sign_in(client, portal, "acct-alex", "alex")
    assert response.headers["Location"].endswith("/platform-oauth/connect")
    assert not signed_in(client)
    assert database(wiki).scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    refused = client.post("/platform-oauth/merge", data={"username": "alex", "password": "a guess at it 1"})
    assert refused.status_code == 401 and not signed_in(client)
    assert database(wiki).scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    assert portal.links == {} and alex


def test_a_same_named_account_with_a_1_4_portal_link_is_adopted(build, portal):
    wiki = build(stored="platform")
    alex = user_in(wiki, "alex")
    portal.links[alex["id"]] = "acct-alex"  # known only to the portal
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-alex", "alex")
    assert signed_in(client)
    assert database(wiki).scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-alex'") \
        == alex["id"]


def test_a_same_named_account_linked_to_someone_else_is_not_taken_over(build, portal):
    wiki = build(stored="platform")
    alex = user_in(wiki, "alex")
    link(wiki, portal, alex, "acct-real-alex")
    client = wiki.test_client()
    response = platform_sign_in(client, portal, "acct-impostor", "alex")
    assert response.headers["Location"].endswith("/platform-oauth/connect")
    page = client.get("/platform-oauth/connect").get_data(as_text=True)
    assert 'value="alex2"' in page, "the name is not offered"
    refused = client.post("/platform-oauth/merge", data={"username": "alex", "password": PASSWORD})
    assert refused.status_code == 409 and not signed_in(client)
    db = database(wiki)
    assert db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-real-alex'") == alex["id"]
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links WHERE account_id = 'acct-impostor'") == 0


def test_a_hosting_account_reaches_only_its_own_wiki_account(build, portal):
    wiki = build(stored="platform")
    alex, other = user_in(wiki, "alex"), user_in(wiki, "other")
    link(wiki, portal, alex, "acct-alex")
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-alex", "other")  # renamed on the portal to someone else's name
    assert signed_in(client)
    db = database(wiki)
    assert db.scalar("SELECT user_id FROM user_sessions WHERE revoked_at IS NULL") == alex["id"]
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links WHERE user_id = ?", (other["id"],)) == 0


def _merge(wiki, source, target, *, delete_source):
    from bananawiki.wiki.features.users import merge

    owner = user_in(wiki, f"boss_{source['username']}", role="owner")
    with wiki.test_request_context(), connection_scope():
        merge.execute(source, target, owner, delete_source=delete_source)


@pytest.mark.parametrize("delete_source", [False, True])
def test_a_merge_keeps_only_the_targets_own_link(build, portal, delete_source):
    wiki = build(stored="platform")
    source, target = user_in(wiki, "old_alex"), user_in(wiki, "alex")
    link(wiki, portal, source, "acct-old")
    link(wiki, portal, target, "acct-new")
    _merge(wiki, source, target, delete_source=delete_source)
    db = database(wiki)
    assert db.all("SELECT account_id, user_id FROM platform_oauth_links") == [
        {"account_id": "acct-new", "user_id": target["id"]}]
    client = wiki.test_client()
    platform_sign_in(client, portal, "acct-new", "alex")
    assert db.scalar("SELECT user_id FROM user_sessions WHERE revoked_at IS NULL") == target["id"]


def test_a_merge_never_hands_the_sources_link_to_the_target(build, portal):
    wiki = build(stored="platform")
    source, target = user_in(wiki, "old_alex"), user_in(wiki, "alex")
    link(wiki, portal, source, "acct-old")
    _merge(wiki, source, target, delete_source=False)
    db = database(wiki)
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links") == 0
    assert portal.links == {}, "the portal was told to drop the locked account's link"
    # The person behind the source's hosting account connects the merged account with its password.
    client = wiki.test_client()
    response = platform_sign_in(client, portal, "acct-old", "old_alex")
    assert response.headers["Location"].endswith("/platform-oauth/connect")
    assert not signed_in(client)
    assert client.post("/platform-oauth/merge", data={"username": "alex", "password": PASSWORD}).status_code == 302
    assert db.scalar("SELECT user_id FROM platform_oauth_links WHERE account_id = 'acct-old'") == target["id"]


def test_a_merge_survives_a_portal_that_cannot_drop_the_link(build, portal, monkeypatch):
    wiki = build(stored="platform")
    source, target = user_in(wiki, "old_alex"), user_in(wiki, "alex")
    link(wiki, portal, source, "acct-old")

    def unreachable(*_args, **_kwargs):
        raise platform_oauth.OAuthError("auth.oauth.error.portal", "down")

    monkeypatch.setattr(platform_oauth, "portal_unlink", unreachable)
    _merge(wiki, source, target, delete_source=False)
    db = database(wiki)
    assert db.scalar("SELECT suspended FROM users WHERE id = ?", (source["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) FROM platform_oauth_links WHERE user_id = ?", (target["id"],)) == 0
