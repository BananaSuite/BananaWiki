"""The `banana` controller CLI, the `bananawiki` admin CLI, the WSGI/Gunicorn/TTS shims."""

from __future__ import annotations

import importlib
import json
import runpy
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananawiki import cli as admin
from bananawiki.ops import cli as ops_cli
from bananawiki.ops import tenant_entrypoint, tts_worker

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"  # noqa: S105 - test fixture


def compat(relative: str) -> Path:
    """A 1.4 entry point at the repository root (banana, wsgi.py, gunicorn.conf.py)."""
    return REPO / relative


# Controller CLI -------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["install", "--mode", "wiki", "--domain", "w.example.org", "--repo", "https://github.com/o/r.git"],
    ["install", "--mode", "hosting", "--name", "platform", "--token-file", "/t", "--require-signatures", "/s"],
    ["install", "--restore", "/p.tar.gz", "--reuse-data"],
    ["update", "--automatic"], ["update", "--allow-divergent", "--retry-failed"],
    ["backup", "--output", "/b.tar.gz"], ["migrate", "--output", "/b.tar.gz"],
    ["restore", "/p.tar.gz", "--domain", "x.org", "--port", "5005"], ["rollback"], ["rollback", "--package", "/p"],
    ["status"], ["start"], ["stop"], ["restart"], ["recover"], ["recover", "--abandon"],
    ["updates", "enable", "--interval", "30", "--keep-backups", "5"], ["updates", "disable"],
    ["source", "set", "--repo", "git@github.com:o/r.git", "--ssh-key", "/k", "--known-hosts", "/h"],
    ["source", "check"], ["source", "show"], ["source", "set", "--clear-credentials", "--clear-signatures"],
    ["proxy", "--install", "--replace", "--email", "a@b.org"], ["uninstall", "--purge", "--confirm", "bananawiki"],
    ["backups", "status"], ["backups", "run", "--automatic"], ["agent", "serve"], ["agent", "status"],
    ["reset-password", "alice", "--generate"], ["db", "check"],
])
def test_controller_keeps_the_1x_command_surface(argv):
    args = ops_cli.parser().parse_args(["--root", "/opt/bananawiki", *argv])
    assert args.command == argv[0]


def test_controller_requires_root(monkeypatch, capsys):
    monkeypatch.setattr(ops_cli.os, "geteuid", lambda: 1000)
    assert ops_cli.main(["status"]) == 1
    assert "needs root" in capsys.readouterr().err


def test_controller_reports_errors_and_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops_cli.os, "geteuid", lambda: 0)
    assert ops_cli.main(["--root", str(tmp_path / "opt/none"), "status"]) == 1
    assert "No matching managed installation" in capsys.readouterr().err


def test_banana_entry_point_is_stdlib_only():
    """The installed wrapper runs `current/banana` with the system Python, outside the venv."""
    code = (
        "import sys, runpy; sys.argv = ['banana', '--help']\n"
        "blocked = {'flask', 'werkzeug', 'jinja2', 'filelock', 'cryptography'}\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in blocked: raise ImportError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        f"runpy.run_path({str(compat('banana'))!r}, run_name='__main__')\n"
    )
    import subprocess

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, result.stderr
    assert "install" in result.stdout and "rollback" in result.stdout


# Admin CLI ------------------------------------------------------------------------


@pytest.fixture
def wiki_env(tmp_path, monkeypatch):
    environ = {"BW_ENV": "test", "BW_INSTANCE_DIR": str(tmp_path / "instance"), "BW_SETUP_TOKEN": "tok-123",
               "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000", "SECRET_KEY": "k" * 40, "BW_BACKGROUND_JOBS": "0"}
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BW_DATABASE_PATH", raising=False)  # set by the 1.4 root conftest
    password = tmp_path / "password"
    password.write_text(PASSWORD + "\n")
    return tmp_path


def run_admin(capsys, *argv: str) -> tuple[int, dict | str]:
    code = admin.main(list(argv))
    out = capsys.readouterr()
    try:
        return code, json.loads(out.out)
    except json.JSONDecodeError:
        return code, out.out + out.err


def test_lifecycle_commands_are_dispatched_to_the_controller():
    assert admin.is_lifecycle(["update"]) and admin.is_lifecycle(["--root", "/opt/x", "migrate"])
    assert admin.is_lifecycle(["backup", "--output", "x"])
    assert not admin.is_lifecycle(["migrate"]) and not admin.is_lifecycle(["db", "backup"])


def test_admin_accounts_and_database(wiki_env, capsys):
    code, output = run_admin(capsys, "migrate")
    assert code == 0 and output["schema_version"] >= 4
    code, output = run_admin(capsys, "setup-token")
    assert code == 0 and "tok-123" in output if isinstance(output, str) else output == "tok-123"
    code, output = run_admin(capsys, "create-admin", "alice", "--password-file", str(wiki_env / "password"))
    assert code == 0 and output["role"] == "admin"
    code, output = run_admin(capsys, "create-admin", "alice", "--password-file", str(wiki_env / "password"))
    assert code == 1
    code, output = run_admin(capsys, "setup-token")
    assert code == 1  # setup is complete once an administrator exists
    code, output = run_admin(capsys, "reset-password", "alice", "--generate")
    assert code == 0 and output["must_change"] and len(output["password"]) >= 12
    code, output = run_admin(capsys, "reset-password", "nobody", "--generate")
    assert code == 1
    database = sqlite3.connect(wiki_env / "instance/bananawiki.db")
    row = database.execute("SELECT role, force_password_change FROM users WHERE username='alice'").fetchone()
    database.close()
    assert row == ("admin", 1)
    code, output = run_admin(capsys, "db", "check")
    assert code == 0 and output["integrity"] == "ok"
    code, output = run_admin(capsys, "db", "backup", "--output", str(wiki_env / "copy.db"))
    assert code == 0 and Path(output["backup"]).is_file()
    code, output = run_admin(capsys, "config", "check")
    assert code == 0 and output["environment"] == "test"
    code, output = run_admin(capsys, "jobs", "list")
    assert code == 0 and isinstance(output, list)
    code, output = run_admin(capsys, "jobs", "run", "no-such-job")
    assert code == 1


def test_prune_retired_plugin_tables(wiki_env, capsys):
    run_admin(capsys, "migrate")
    database = sqlite3.connect(wiki_env / "instance/bananawiki.db")
    database.execute("CREATE TABLE IF NOT EXISTS banana_ai__chats (id INTEGER PRIMARY KEY)")
    database.execute("CREATE TABLE IF NOT EXISTS meetings__rooms (id INTEGER PRIMARY KEY)")
    database.commit()
    database.close()
    code, output = run_admin(capsys, "db", "prune-retired")
    assert code == 1  # not interactive and no --yes: nothing happens
    code, output = run_admin(capsys, "db", "prune-retired", "--yes")
    assert code == 0 and {"banana_ai__chats", "meetings__rooms"} <= set(output["dropped"])
    assert Path(output["backup"]).is_file()
    database = sqlite3.connect(wiki_env / "instance/bananawiki.db")
    remaining = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    database.close()
    assert "banana_ai__chats" not in remaining and "users" in remaining and "pages" in remaining


def test_export_import_round_trip(wiki_env, capsys, monkeypatch):
    run_admin(capsys, "create-admin", "bob", "--password-file", str(wiki_env / "password"))
    (wiki_env / "instance/uploads").mkdir(exist_ok=True)
    (wiki_env / "instance/uploads/a.png").write_bytes(b"png")
    code, output = run_admin(capsys, "export", "--output", str(wiki_env / "export.tar.gz"))
    assert code == 0
    monkeypatch.setenv("BW_INSTANCE_DIR", str(wiki_env / "second"))
    code, output = run_admin(capsys, "import", str(wiki_env / "export.tar.gz"))
    assert code == 0
    assert (wiki_env / "second/uploads/a.png").read_bytes() == b"png"
    database = sqlite3.connect(wiki_env / "second/bananawiki.db")
    assert database.execute("SELECT username FROM users").fetchall() == [("bob",)]
    database.close()
    code, _ = run_admin(capsys, "import", str(wiki_env / "export.tar.gz"))
    assert code == 1  # never into a non-empty instance


def test_serve_refuses_the_debugger_off_loopback(wiki_env, capsys):
    code, output = run_admin(capsys, "serve", "--host", "0.0.0.0", "--debug")  # noqa: S104
    assert code == 1 and "loopback" in output
    assert admin._is_loopback("127.0.0.1") and admin._is_loopback("::1") and not admin._is_loopback("10.0.0.1")


# Runtime shims ----------------------------------------------------------------------


def test_gunicorn_settings(monkeypatch):
    monkeypatch.setenv("BW_HOST", "::")
    monkeypatch.setenv("BW_PORT", "6000")
    monkeypatch.setenv("BW_WORKERS", "999")
    import bananawiki.ops.gunicorn_conf as conf

    conf = importlib.reload(conf)
    assert conf.bind == "[::]:6000" and conf.workers == 16 and conf.preload_app is False
    assert conf.control_socket_disable is True
    namespace = runpy.run_path(str(compat("gunicorn.conf.py")))
    assert namespace["bind"] == "[::]:6000" and namespace["worker_class"] == "gthread"


def test_gunicorn_workers_give_up_on_stalled_downloads(monkeypatch):
    """Gunicorn has no write timeout of its own: the workers' application sets one (R-27)."""
    import bananawiki.ops.gunicorn_conf as conf

    monkeypatch.setenv("BW_WRITE_TIMEOUT", "15")
    conf = importlib.reload(conf)
    timeouts = []
    worker = SimpleNamespace(wsgi=lambda _environ, _start: [b"body"])
    hook = runpy.run_path(str(compat("gunicorn.conf.py")))["post_worker_init"]
    hook(worker)
    environ = {"gunicorn.socket": SimpleNamespace(settimeout=timeouts.append)}
    assert list(worker.wsgi(environ, None)) == [b"body"] and timeouts == [15]
    monkeypatch.setenv("BW_WRITE_TIMEOUT", "")
    assert importlib.reload(conf)._write_timeout == 300


def test_the_gunicorn_master_upgrades_the_database_before_any_worker_starts(wiki_env, caplog):
    """A worker that upgraded a large database was killed by its timeout, rolling the upgrade back (R-26)."""
    from bananawiki.wiki import migrations

    path = wiki_env / "instance" / "bananawiki.db"
    path.parent.mkdir()
    legacy = sqlite3.connect(path)  # as 1.4 left it: schema version 3
    legacy.executescript(migrations._BASELINE_SQL.read_text(encoding="utf-8"))
    legacy.execute(f"PRAGMA application_id={migrations.APPLICATION_ID}")
    legacy.execute("PRAGMA user_version=3")
    legacy.commit()
    legacy.close()
    import bananawiki.ops.gunicorn_conf as conf

    conf = importlib.reload(conf)
    conf.on_starting(SimpleNamespace(log=SimpleNamespace(info=lambda *_args: None)))
    upgraded = sqlite3.connect(path)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
    upgraded.close()
    assert len(list((wiki_env / "instance" / "backups").glob("pre-upgrade-v3-*.db"))) == 1
    # The managed units and Compose run the root gunicorn.conf.py: Gunicorn takes the hook from it.
    hook = runpy.run_path(str(compat("gunicorn.conf.py")))["on_starting"]
    gunicorn_config = pytest.importorskip("gunicorn.config")
    settings = gunicorn_config.Config()
    settings.set("on_starting", hook)
    assert settings.on_starting is hook


def test_wsgi_shim_serves_health_during_maintenance(wiki_env, monkeypatch):
    marker = wiki_env / "maintenance"
    marker.write_text("x")
    monkeypatch.setenv("BANANA_MAINTENANCE_FILE", str(marker))
    sys.modules.pop("bananawiki.ops.wsgi", None)
    namespace = runpy.run_path(str(compat("wsgi.py")))
    client = namespace["app"].test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 503
    sys.modules.pop("bananawiki.ops.wsgi", None)


def test_tts_worker_idles_without_a_worker(monkeypatch):
    monkeypatch.setattr(tts_worker, "_worker", lambda: None)
    assert tts_worker.main(["--once"]) == 0
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(tts_worker.signal, "signal", lambda *_: None)
    assert tts_worker.main([], stop=stop) == 0
    calls = []
    monkeypatch.setattr(tts_worker, "_worker", lambda: lambda argv: calls.append(argv) or 0)
    assert tts_worker.main(["--poll-interval", "5"]) == 0 and calls == [["--poll-interval", "5"]]


def test_tenant_entrypoint_commands():
    web = tenant_entrypoint.web_command({"BW_INSTANCE_GUNICORN_WORKERS": "2"})
    assert web[1:5] == ["-m", "gunicorn", "-c", "python:bananawiki.ops.gunicorn_conf"]
    assert web[-1] == "bananawiki.ops.wsgi:app" and "2" in web
    assert tenant_entrypoint.tts_command({})[1:3] == ["-m", "bananawiki.ops.tts_worker"]


def test_tenant_supervisor_stops_when_web_never_listens(monkeypatch):
    class Process:
        returncode = 3

        def poll(self):
            return 3

        def wait(self):
            return 3

        def terminate(self):
            pass

    spawned = []
    supervisor = tenant_entrypoint.Supervisor({"BW_PORT": "1", "BW_INSTANCE_STARTUP_TIMEOUT_SECONDS": "30"},
                                              spawn=lambda command, env: spawned.append(command) or Process())
    assert supervisor.run() == 3
    assert len(spawned) == 1, "the TTS worker only starts after the web process is ready"
