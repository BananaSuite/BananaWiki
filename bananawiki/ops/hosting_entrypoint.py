"""Install the hosting service's quota boundary before loading the application.

The portal owns tenant files on the host, so it must not issue XFS project
setters. Python itself requires descriptor and socket ioctls; a syscall-wide
systemd denial would prevent ordinary files, sockets and Gunicorn from working.
This inherited native seccomp filter permits only those three safe commands.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys

_AUDIT_ARCH_X86_64 = 0xC000003E
_IOCTL = 16
_X32 = 0x40000000
_ALLOW = 0x7FFF0000
_DENY = 0x00050000 | errno.EPERM
_KILL = 0x80000000
_SAFE_IOCTLS = (0x5421, 0x5451, 0x5450)  # FIONBIO, FIOCLEX, FIONCLEX.


class _Instruction(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class _Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_Instruction))]


def _instructions() -> list[tuple[int, int, int, int]]:
    # ioctl's kernel request is an unsigned int: inspecting its low 32 bits
    # denies filesystem setters even when callers supply upper-bit aliases.
    # x32 syscall numbers are rejected before the native ioctl comparison.
    return [
        (0x20, 0, 0, 4),                 # LD architecture.
        (0x15, 1, 0, _AUDIT_ARCH_X86_64),
        (0x06, 0, 0, _KILL),
        (0x20, 0, 0, 0),                 # LD syscall number.
        (0x35, 0, 1, _X32),
        (0x06, 0, 0, _DENY),
        (0x15, 0, 5, _IOCTL),
        (0x20, 0, 0, 24),                # LD request (args[1]).
        (0x15, 3, 0, _SAFE_IOCTLS[0]),
        (0x15, 2, 0, _SAFE_IOCTLS[1]),
        (0x15, 1, 0, _SAFE_IOCTLS[2]),
        (0x06, 0, 0, _DENY),
        (0x06, 0, 0, _ALLOW),
    ]


def install_filter() -> None:
    """Fail closed unless the inherited x86-64 Linux filter is installed."""
    if (sys.platform != "linux" or platform.machine() != "x86_64"
            or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_ulong) != 8):
        raise RuntimeError("The managed hosting sandbox requires x86-64 Linux.")
    try:
        library = ctypes.CDLL(None, use_errno=True)
        prctl = library.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        instructions = _instructions()
        native = (_Instruction * len(instructions))(*(_Instruction(*item) for item in instructions))
        program = _Program(len(native), native)
        if prctl(38, 1, 0, 0, 0) != 0 or prctl(39, 0, 0, 0, 0) != 1:
            raise RuntimeError("Cannot enforce no-new-privileges for hosting.")
        if prctl(22, 2, ctypes.addressof(program), 0, 0) != 0 or prctl(21, 0, 0, 0, 0) != 2:
            raise RuntimeError("Cannot install the hosting ioctl filter.")
    except (AttributeError, OSError) as error:
        raise RuntimeError("Cannot install the hosting ioctl filter.") from error


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (["portal"], ["maintenance"]):
        print("Usage: hosting_entrypoint {portal|maintenance}", file=sys.stderr)
        return 2
    try:
        install_filter()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 78
    command = (["-m", "gunicorn", "-c", "hosting/gunicorn.conf.py", "--timeout", "180", "hosting.wsgi:app"]
               if arguments == ["portal"] else ["-m", "hosting.maintenance", "--interval", "300"])
    # The interpreter and module/arguments are fixed trusted release paths.
    os.execv(sys.executable, [sys.executable, "-E", "-s", *command])  # noqa: S606
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
