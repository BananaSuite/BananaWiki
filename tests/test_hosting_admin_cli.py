import json
import os
import subprocess
import sys


def _run_cli(tmp_path, *args):
    env = os.environ.copy()
    env["HOSTING_SECRET_KEY"] = "test-hosting-cli-secret"
    env["HOSTING_DATABASE_PATH"] = str(tmp_path / "hosting.db")
    env["INSTANCES_DIR"] = str(tmp_path / "instances")
    return subprocess.run(
        [sys.executable, "-m", "hosting.admin_cli", "--json", *args],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_can_create_and_show_hosting_account(tmp_path):
    result = _run_cli(tmp_path, "account", "create", "cliadmin", "password123", "--admin")
    assert result.returncode == 0, result.stderr
    created = json.loads(result.stdout)
    assert created["username"] == "cliadmin"
    assert created["is_admin"] is True

    result = _run_cli(tmp_path, "account", "show", "cliadmin")
    assert result.returncode == 0, result.stderr
    shown = json.loads(result.stdout)
    assert shown["account"]["id"] == created["id"]
    assert shown["account"]["is_admin"] == 1


def test_cli_updates_signup_mode_and_invites(tmp_path):
    result = _run_cli(tmp_path, "settings", "signup-mode", "invite")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["signup_mode"] == "invite"

    result = _run_cli(tmp_path, "invite", "create", "--note", "ssh", "--max-uses", "2")
    assert result.returncode == 0, result.stderr
    invite = json.loads(result.stdout)
    assert invite["note"] == "ssh"
    assert invite["max_uses"] == 2

    result = _run_cli(tmp_path, "invite", "list")
    assert result.returncode == 0, result.stderr
    invites = json.loads(result.stdout)
    assert len(invites) == 1
    assert invites[0]["code"] == invite["code"]


def test_cli_requires_yes_for_destructive_account_delete(tmp_path):
    created = json.loads(
        _run_cli(tmp_path, "account", "create", "cliuser", "password123").stdout
    )

    result = _run_cli(tmp_path, "account", "delete", "cliuser")
    assert result.returncode == 2
    assert "Re-run with --yes" in result.stderr

    result = _run_cli(tmp_path, "account", "show", created["id"])
    assert result.returncode == 0, result.stderr
