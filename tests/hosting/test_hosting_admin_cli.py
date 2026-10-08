"""The operator command line (``python -m bananawiki.hosting.admin``) and the maintenance loop."""

from __future__ import annotations

import io
import json
import signal
import sys
from pathlib import Path

import pytest

from bananawiki.hosting import admin, maintenance

from .hosting_support import PASSWORD


@pytest.fixture
def cli(portal, capsys, monkeypatch):
    def run(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = admin.main(list(argv), app=portal)
        out, err = capsys.readouterr()
        return code, out, err

    return run


def _audit(portal) -> list[dict]:
    path = Path(portal.config["HOSTING"].database_path).parent / admin.AUDIT_FILE
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_passwords_are_never_command_line_arguments(cli):
    with pytest.raises(SystemExit):
        admin.parser().parse_args(["account", "create", "alice", "secret-password"])
    with pytest.raises(SystemExit):
        admin.parser().parse_args(["account", "set-password", "alice", "secret-password"])


def test_account_create_from_stdin_and_file(cli, portal, tmp_path, query):
    code, out, _err = cli("account", "create", "alice", "--admin", stdin=PASSWORD + "\n")
    assert code == 0 and "Created account alice" in out
    secret = tmp_path / "pw"
    secret.write_text("another password 7\n")
    assert cli("account", "set-password", "alice", "--password-file", str(secret))[0] == 0
    row = query("SELECT is_admin, password FROM accounts WHERE username = 'alice'", one=True)
    assert row["is_admin"] == 1 and row["password"] != "another password 7"
    entries = _audit(portal)
    assert [entry["action"] for entry in entries] == ["account.create", "account.set_password"]
    assert "another password 7" not in json.dumps(entries) and PASSWORD not in json.dumps(entries)


def test_output_hides_password_hashes_and_secrets(cli, make_account):
    make_account("bob")
    code, out, _err = cli("--json", "account", "show", "bob")
    data = json.loads(out)
    assert code == 0 and data["account"]["username"] == "bob"
    assert not any("password" in key or "secret" in key for key in data["account"])


def test_service_errors_are_translated(cli, make_account):
    make_account("carol")
    code, _out, err = cli("account", "create", "carol", stdin=PASSWORD + "\n")
    assert code == 1 and err.startswith("hosting-admin: ") and "hosting." not in err
    code, _out, err = cli("instance", "show", "nowhere")
    assert code == 1 and "Wiki not found" in err


def test_instance_commands(cli, make_account, make_wiki, runtime, query):
    owner = make_account("dave")
    wiki = make_wiki(owner, "daves-wiki")
    code, out, _err = cli("--json", "instance", "list", "--account", "dave")
    assert code == 0 and [row["subdomain"] for row in json.loads(out)] == ["daves-wiki"]
    assert cli("instance", "stop", "daves-wiki")[0] == 0
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "stopped"
    code, out, _err = cli("--json", "instance", "reset-password", wiki["id"])
    assert code == 0 and json.loads(out)["password"]
    assert cli("instance", "extend", "daves-wiki", "3")[0] == 0
    code, _out, err = cli("instance", "terminate", "daves-wiki")
    assert code == 1 and "--yes" in err
    assert cli("instance", "terminate", "daves-wiki", "--yes")[0] == 0
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],), one=True)["status"] == "terminated"


def test_wiki_user_commands_use_the_runtime(cli, make_account, make_wiki, runtime):
    wiki = make_wiki(make_account("erin"), "erins")
    assert cli("instance", "set-user-password", "erins", "frank", "--role", "editor",
               stdin="frank-password\n")[0] == 0
    code, out, _err = cli("--json", "instance", "users", "erins")
    assert code == 0 and {"username": "frank", "role": "editor", "created_at": "2026-01-01 00:00:00"} in \
        json.loads(out)["users"]
    assert runtime.tenants[wiki["subdomain"]].passwords["frank"] == "frank-password"
    assert cli("instance", "remove-user", "erins", "frank", "--yes")[0] == 0


def test_invites_and_signup_mode(cli, query):
    code, out, _err = cli("--json", "invite", "create", "--max-uses", "3")
    invite = json.loads(out)
    assert code == 0 and invite["max_uses"] == 3
    assert cli("settings", "signup-mode", "invite")[0] == 0
    assert query("SELECT signup_mode FROM hosting_settings", one=True)["signup_mode"] == "invite"
    assert cli("invite", "delete", str(invite["id"]))[0] == 0
    assert cli("invite", "delete", str(invite["id"]))[0] == 1


def test_dates_outside_the_supported_years_are_refused(cli, make_account, make_wiki, query):
    make_wiki(make_account("hank"), "hanks")
    code, _out, err = cli("instance", "set-expiry", "hanks", "9999-12-31T00:00:00")
    assert code == 1 and "between the years 1900 and 9998" in err
    code, _out, err = cli("instance", "suspend", "hanks", "--until", "9999-12-31T00:00:00Z")
    assert code == 1 and "between the years 1900 and 9998" in err
    code, _out, err = cli("instance", "suspend", "hanks", "--hours", "99999999999")
    assert code == 1 and "too large" in err
    assert query("SELECT status FROM instances", one=True)["status"] == "running"
    query("UPDATE instances SET expires_at = '9999-12-31 00:00:00'")
    code, _out, err = cli("instance", "extend", "hanks", "30")
    assert code == 1 and "9998" in err
    assert cli("instance", "set-expiry", "hanks", "2100-01-01T00:00:00+02:00")[0] == 0
    assert query("SELECT expires_at FROM instances", one=True)["expires_at"] == "2099-12-31 22:00:00"


def test_audit_failure_is_reported_not_swallowed(cli, portal, monkeypatch):
    monkeypatch.setattr(admin, "AUDIT_FILE", "missing-dir/audit.log")
    code, _out, err = cli("account", "create", "gina", stdin=PASSWORD + "\n")
    assert code == 0 and "not written to the audit log" in err


def test_ops_forwards_hosting_admin_as_the_service_account(tmp_path, monkeypatch):
    from bananawiki.ops import cli as ops_cli

    class Manager:
        root = tmp_path
        config_dir = tmp_path / "config"

        def settings(self):
            return {"mode": "hosting", "service": "svc"}

    Manager.config_dir.mkdir()
    (Manager.config_dir / "app.env").write_text("")
    seen = {}
    monkeypatch.setattr(ops_cli.subprocess, "run", lambda argv, **_: seen.setdefault("argv", argv) and
                        type("R", (), {"returncode": 0})())
    assert ops_cli.run_app_command(Manager(), "hosting-admin", ["status"]) == 0
    assert seen["argv"][:4] == ["runuser", "-u", "svc", "--"]
    assert seen["argv"][-3:] == ["-m", "bananawiki.hosting.admin", "status"]
    with pytest.raises(ValueError):
        ops_cli.run_app_command(Manager(), "create-admin", [])


# ── Maintenance loop ──────────────────────────────────────────────────────────


def test_run_recovers_first_then_ticks_until_sigterm(portal, runtime, monkeypatch):
    order = []
    monkeypatch.setattr(maintenance, "recover", lambda app: order.append("recover") or 0)
    ticks = []

    def once(app):
        order.append("pass")
        ticks.append(1)
        if len(ticks) == 2:
            maintenance._request_stop(signal.SIGTERM, None)
        return {}

    monkeypatch.setattr(maintenance, "run_once", once)
    monkeypatch.setattr(maintenance._stop, "wait", lambda _seconds: maintenance._stop.is_set())
    maintenance._stop.clear()
    try:
        assert maintenance.run(portal, interval=30) == 0
    finally:
        maintenance._stop.clear()
    assert order == ["recover", "pass", "pass"]


def test_sigterm_ends_a_pass_between_steps(portal, runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance.instances, "lift_expired_suspensions",
                        lambda: calls.append("first") or maintenance._stop.set())
    maintenance._stop.clear()
    try:
        results = maintenance.run_once(portal)
    finally:
        maintenance._stop.clear()
    assert calls == ["first"] and list(results) == ["instance suspensions"]
