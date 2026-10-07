"""No tenant code runs until release inside the verified container namespace."""
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bananawiki.ops import tenant_guard as guard

TOKEN = "a1" * 16


@pytest.fixture(autouse=True)
def mock_seal(monkeypatch):
    # The real native boundary is exercised in its own subprocess below.
    monkeypatch.setattr(guard, "_seal_process", lambda: None)


def test_actual_abstract_socket_release_has_no_filesystem_side_effects(tmp_path, monkeypatch):
    token = secrets.token_hex(16)
    answers = []
    monkeypatch.chdir(tmp_path)
    thread = threading.Thread(target=lambda: answers.append(guard.wait(token, lifetime=2)))
    thread.start()
    try:
        guard.release(token, lifetime=1)
        thread.join(timeout=2)
        assert answers == [True]
        assert list(tmp_path.iterdir()) == []
        with pytest.raises(TimeoutError):
            guard.release(token, lifetime=0.1)
    finally:
        thread.join(timeout=3)


def test_server_never_executes_without_release(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "wait", lambda _token: False)
    monkeypatch.setattr(guard.os, "execv", lambda *args: calls.append(args))
    assert guard.main(["server", TOKEN]) == 124
    assert calls == []


def test_verified_server_preserves_fixed_existing_entrypoint(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "wait", lambda _token: True)
    monkeypatch.setattr(guard.os, "execv", lambda *args: calls.append(args))
    assert guard.main(["server", TOKEN]) == 0
    assert calls == [(sys.executable, [sys.executable, "-E", "-s", "-m", "bananawiki.ops.tenant_entrypoint"])]


def test_task_never_opens_release_socket_or_executes(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "_idle", lambda seconds: calls.append(seconds))
    monkeypatch.setattr(guard, "wait", lambda _token: pytest.fail("Task opened a release gate"))
    monkeypatch.setattr(guard.os, "execv", lambda *_args: pytest.fail("Task executed tenant code"))
    assert guard.main(["task", TOKEN, "630"]) == 124
    assert calls == [630]


def test_task_default_lifetime(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "_idle", lambda seconds: calls.append(seconds))
    assert guard.main(["task", TOKEN]) == 124
    assert calls == [140]


@pytest.mark.parametrize("mode", ["server", "task"])
def test_process_seal_failure_never_opens_gate_or_executes(mode, monkeypatch):
    def refuse():
        raise OSError("Native isolation unavailable")
    monkeypatch.setattr(guard, "_seal_process", refuse)
    monkeypatch.setattr(guard, "wait", lambda *_args: pytest.fail("Unsealed server opened its gate"))
    monkeypatch.setattr(guard, "_idle", lambda *_args: pytest.fail("Unsealed task waited"))
    assert guard.main([mode, TOKEN]) == 1


@pytest.mark.parametrize("token", ["", "A1" * 16, "a" * 31, "a" * 33, "../" + "a" * 29, "a" * 31 + "\n"])
def test_invalid_token_cannot_connect_to_release_socket(token, monkeypatch):
    monkeypatch.setattr(guard.socket, "socket", lambda *_args: pytest.fail("Invalid token reached socket"))
    assert guard.main(["release", token]) == 1


@pytest.mark.parametrize("seconds", ["0", "4", "631", "6000", "005", "5.0", "-5", "５"])
def test_invalid_lifetime_does_not_enter_wait(seconds, monkeypatch):
    monkeypatch.setattr(guard, "_idle", lambda *_args: pytest.fail("Invalid task lifetime admitted"))
    assert guard.main(["task", TOKEN, seconds]) == 1


@pytest.mark.parametrize("arguments", [[], ["server"], ["unknown", TOKEN], ["server", TOKEN, "anything"],
                                         ["release", TOKEN, "anything"], ["task", TOKEN, "5", "anything"]])
def test_invalid_mode_or_extra_arguments_refused(arguments):
    assert guard.main(arguments) == 2


@pytest.mark.parametrize("payload", [b"", b"false-verified\n", guard._RELEASE + b"x"])
def test_invalid_bounded_release_message_keeps_server_inert(payload):
    token = secrets.token_hex(16)
    errors = []
    def receive():
        try:
            guard.wait(token, lifetime=2)
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=receive)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            deadline = time.monotonic() + 1
            while True:
                try:
                    client.connect(guard._address(token))
                    break
                except ConnectionRefusedError:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            client.sendall(payload)
        thread.join(timeout=2)
        assert errors == ["The tenant mount gate release message is invalid"]
    finally:
        thread.join(timeout=3)


def test_unreleased_socket_times_out_without_execution():
    assert not guard.wait(secrets.token_hex(16), lifetime=0.05)


@pytest.mark.parametrize("failure", ["set", "readback"])
def test_native_process_isolation_failure_is_closed(failure, monkeypatch):
    calls = []
    class Call:
        def __call__(self, *arguments):
            calls.append(arguments)
            return -1 if failure == "set" and arguments[0] == 4 else 1 if arguments[0] == 3 else 0
    class Native:
        prctl = Call()
    monkeypatch.undo()
    monkeypatch.setattr(guard.ctypes, "CDLL", lambda *_args, **_kwargs: Native())
    with pytest.raises((OSError, ValueError)):
        guard._seal_process()
    assert calls[0] == (4, 0, 0, 0, 0)
    if failure == "readback":
        assert calls[1] == (3, 0, 0, 0, 0)


def test_guard_refuses_unsupported_process_isolation(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(guard.sys, "platform", "unsupported")
    with pytest.raises(ValueError, match="Linux"):
        guard._seal_process()


def test_actual_same_uid_proc_root_is_denied_while_task_stays_inert(tmp_path):
    if sys.platform != "linux" or os.geteuid() == 0:
        pytest.skip("Requires ordinary Linux UID without CAP_SYS_PTRACE")
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.env").write_text("This must never be read by the guard")
    process = subprocess.Popen([sys.executable, "-E", "-s", "-m", "bananawiki.ops.tenant_guard",
                                "task", TOKEN, "630"], env={**os.environ, "BW_INSTANCE_DIR": str(data)},
                               cwd=Path(__file__).parents[2])
    try:
        deadline = time.monotonic() + 2
        while True:
            assert process.poll() is None
            try:
                os.readlink(f"/proc/{process.pid}/root")
            except PermissionError:
                break
            assert time.monotonic() < deadline, "Guard did not disable same-UID proc access"
            time.sleep(0.01)
        process.terminate()
        assert process.wait(timeout=3) == 143
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    assert [path.name for path in data.iterdir()] == ["config.env"]
