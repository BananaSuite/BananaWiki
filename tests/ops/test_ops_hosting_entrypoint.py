"""Inherited kernel enforcement and benign hosting interpreter compatibility."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from bananawiki.ops import hosting_entrypoint as entrypoint
from bananawiki.ops import profile


@pytest.mark.skipif(sys.platform != "linux" or entrypoint.platform.machine() != "x86_64",
                    reason="Managed hosting requires x86-64 Linux")
def test_native_filter_preserves_files_sockets_and_denies_quota_setters(tmp_path):
    path = tmp_path / "ordinary-file"
    path.write_text("ordinary read")
    script = r'''
import ctypes, fcntl, json, os, socket, subprocess, sys
from bananawiki.ops.hosting_entrypoint import install_filter
install_filter()
assert open(sys.argv[1]).read() == "ordinary read"
sock = socket.socket(socket.AF_UNIX)
sock.settimeout(1)
sock.setblocking(True)
sock.close()
fd = os.open(sys.argv[1], os.O_RDONLY)
fcntl.ioctl(fd, 0x5450)
assert not fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
fcntl.ioctl(fd, 0x5451)
assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
denied = []
for request in (0x401c5820, 0x40086602, 0x40046602, 0x401c5820 | (1 << 32),
                0x40086602 | (1 << 63), 0x801c581f):
    try:
        fcntl.ioctl(fd, request, bytes(28))
    except OSError as error:
        assert error.errno == 1, (request, error.errno)
        denied.append(request)
    else:
        raise AssertionError("An unapproved ioctl reached the kernel")
library = ctypes.CDLL(None, use_errno=True)
library.syscall.restype = ctypes.c_long
assert library.syscall(ctypes.c_long(0x40000010), ctypes.c_long(fd), ctypes.c_ulong(0x5451), 0) == -1
assert ctypes.get_errno() == 1
child = subprocess.run([sys.executable, "-c", "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDONLY);"
                        "fcntl.ioctl(fd,0x401c5820,bytes(28))", sys.argv[1]], capture_output=True, text=True)
assert child.returncode != 0 and "Operation not permitted" in child.stderr
print(json.dumps({"denied": denied, "exec_inherits": True}))
'''
    completed = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["exec_inherits"]


@pytest.mark.parametrize("arguments", [[], ["other"], ["portal", "extra"], ["maintenance", "../app"]])
def test_invalid_mode_never_installs_or_executes(monkeypatch, arguments):
    monkeypatch.setattr(entrypoint, "install_filter", lambda: pytest.fail("Invalid arguments installed filter"))
    monkeypatch.setattr(entrypoint.os, "execv", lambda *args: pytest.fail("Invalid arguments executed"))
    assert entrypoint.main(arguments) == 2


@pytest.mark.parametrize("mode,module,tail", [
    ("portal", "gunicorn", ["-c", "hosting/gunicorn.conf.py", "--timeout", "180", "hosting.wsgi:app"]),
    ("maintenance", "hosting.maintenance", ["--interval", "300"]),
])
def test_trusted_exec_follows_filter(monkeypatch, mode, module, tail):
    calls = []
    monkeypatch.setattr(entrypoint, "install_filter", lambda: calls.append("filter"))
    monkeypatch.setattr(entrypoint.os, "execv", lambda *args: calls.append(args))
    assert entrypoint.main([mode]) == 78
    assert calls == ["filter", (sys.executable, [sys.executable, "-E", "-s", "-m", module, *tail])]


def test_failed_install_never_executes(monkeypatch):
    def fail():
        raise RuntimeError("Cannot install filter")
    monkeypatch.setattr(entrypoint, "install_filter", fail)
    monkeypatch.setattr(entrypoint.os, "execv", lambda *args: pytest.fail("Failed filter executed"))
    assert entrypoint.main(["portal"]) == 78


@pytest.mark.parametrize("platform_name,machine", [("win32", "x86_64"), ("linux", "aarch64")])
def test_unsupported_native_architecture_refuses(monkeypatch, platform_name, machine):
    monkeypatch.setattr(entrypoint.sys, "platform", platform_name)
    monkeypatch.setattr(entrypoint.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError, match="x86-64 Linux"):
        entrypoint.install_filter()


@pytest.mark.parametrize("failed_call", [38, 39, 22, 21])
def test_native_install_and_readback_failure_refuses(monkeypatch, failed_call):
    class Prctl:
        def __call__(self, operation, *args):
            if operation == failed_call:
                return -1
            return {39: 1, 21: 2}.get(operation, 0)
    class Library:
        prctl = Prctl()
    monkeypatch.setattr(entrypoint.ctypes, "CDLL", lambda *args, **kwargs: Library())
    with pytest.raises(RuntimeError, match="Cannot"):
        entrypoint.install_filter()


def test_missing_native_prctl_refuses(monkeypatch):
    monkeypatch.setattr(entrypoint.ctypes, "CDLL", lambda *args, **kwargs: object())
    with pytest.raises(RuntimeError, match="Cannot install"):
        entrypoint.install_filter()


def test_release_features_preserve_old_target_commands(tmp_path):
    package = tmp_path / "bananawiki/ops"
    package.mkdir(parents=True)
    (package / "runtime_agent.py").write_text("old trusted runtime")
    settings = {"root": "/opt/bananawiki", "service": "bananawiki", "mode": "hosting", "port": 5099}
    old = profile.ReleaseFeatures.of(tmp_path)
    assert old.runtime_agent and old.hardened and not old.hosting_entrypoint
    old_services = profile.services(settings, old)
    assert old_services[1].command[-1] == "hosting.wsgi:app"
    assert old_services[2].command[1:] == ["-m", "hosting.maintenance", "--interval", "300"]
    (package / "hosting_entrypoint.py").write_text("new trusted wrapper")
    current = profile.ReleaseFeatures.of(tmp_path)
    assert current.hosting_entrypoint
    services = profile.services(settings, current)
    assert services[1].command[-2:] == ["bananawiki.ops.hosting_entrypoint", "portal"]
    assert services[2].command[-2:] == ["bananawiki.ops.hosting_entrypoint", "maintenance"]
