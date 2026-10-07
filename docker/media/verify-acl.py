"""Exercise the actual ACL ABI and retained GNU copy/archive programs in an image.

Run as an arbitrary non-root UID with no capabilities, no network, a read-only
root and writable tmpfs /tmp. Tests operate only on their private temporary tree.
Legacy pathname APIs deliberately retain symlink following; this verifies the
new safe APIs and compatibility without claiming those legacy calls became safe.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

ACCESS = 0x8000
DEFAULT = 0x4000
NOFOLLOW = 0x100
EMPTY_PATH = 0x1000
BAD_FLAG = 0x100000
library = ctypes.CDLL("libacl.so.1", use_errno=True)
pointer = ctypes.c_void_p
integer = ctypes.c_int
string = ctypes.c_char_p
for name, arguments, result in [
    ("acl_from_text", [string], pointer),
    ("acl_to_text", [pointer, ctypes.POINTER(ctypes.c_ssize_t)], pointer),
    ("acl_free", [pointer], integer),
    ("acl_get_file", [string, ctypes.c_uint], pointer),
    ("acl_get_file_at", [integer, string, integer, ctypes.c_uint], pointer),
    ("acl_set_file_at", [integer, string, integer, ctypes.c_uint, pointer], integer),
    ("acl_extended_file_at", [integer, string, integer], integer),
    ("acl_delete_def_file_at", [integer, string, integer], integer),
]:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result


def acl_text(fd: int, path: bytes, flags: int, kind: int = ACCESS) -> str:
    acl = library.acl_get_file_at(fd, path, flags, kind)
    assert acl, ("acl_get_file_at", ctypes.get_errno(), path)
    try:
        buffer = library.acl_to_text(acl, None)
        assert buffer
        try:
            return ctypes.string_at(buffer).decode("ascii")
        finally:
            assert library.acl_free(buffer) == 0
    finally:
        assert library.acl_free(acl) == 0


def set_acl(fd: int, path: bytes, flags: int, text: str, kind: int = ACCESS) -> None:
    acl = library.acl_from_text(text.encode("ascii"))
    assert acl, ("acl_from_text", ctypes.get_errno())
    try:
        assert library.acl_set_file_at(fd, path, flags, kind, acl) == 0, ctypes.get_errno()
    finally:
        assert library.acl_free(acl) == 0


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, capture_output=True, timeout=20)


def descriptor_tests(root: Path) -> dict:
    parent = root / "parent"
    victim = root / "victim"
    parent.mkdir()
    victim.mkdir()
    (parent / "file").write_text("intended", encoding="ascii")
    (victim / "file").write_text("unintended", encoding="ascii")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    victim_descriptor = os.open(victim, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    access = "u::rw-,u:12345:r--,g::---,m::r--,o::---"
    other = "u::rw-,u:12345:rw-,g::---,m::rw-,o::---"
    default = "u::rwx,u:12345:r-x,g::---,m::r-x,o::---"
    try:
        set_acl(victim_descriptor, b"file", NOFOLLOW, access)
        set_acl(victim_descriptor, b"", EMPTY_PATH, default, DEFAULT)
        victim_file_before = acl_text(victim_descriptor, b"file", NOFOLLOW)
        victim_default_before = acl_text(victim_descriptor, b"", EMPTY_PATH, DEFAULT)

        # Replace an ancestor with a link after the caller obtains its trusted FD.
        parent.rename(root / "parent-original")
        parent.symlink_to(victim, target_is_directory=True)
        set_acl(descriptor, b"file", NOFOLLOW, other)
        assert "user:12345:rw-" in acl_text(descriptor, b"file", NOFOLLOW)
        assert library.acl_extended_file_at(descriptor, b"file", NOFOLLOW) == 1
        assert acl_text(victim_descriptor, b"file", NOFOLLOW) == victim_file_before

        # The upstream compatibility contract deliberately remains unchanged:
        # a legacy path getter follows the replaced ancestor to the other ACL.
        legacy_acl = library.acl_get_file(os.fsencode(parent / "file"), ACCESS)
        assert legacy_acl
        try:
            legacy_text = library.acl_to_text(legacy_acl, None)
            assert legacy_text
            try:
                assert ctypes.string_at(legacy_text).decode("ascii") == victim_file_before
            finally:
                assert library.acl_free(legacy_text) == 0
        finally:
            assert library.acl_free(legacy_acl) == 0

        file_descriptor = os.open("file", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            assert acl_text(file_descriptor, b"", EMPTY_PATH) == acl_text(descriptor, b"file", NOFOLLOW)
            set_acl(file_descriptor, b"", EMPTY_PATH, access)
            assert library.acl_extended_file_at(file_descriptor, b"", EMPTY_PATH) == 1
        finally:
            os.close(file_descriptor)
        set_acl(descriptor, b"", EMPTY_PATH, default, DEFAULT)
        assert "user:12345:r-x" in acl_text(descriptor, b"", EMPTY_PATH, DEFAULT)
        assert library.acl_delete_def_file_at(descriptor, b"", EMPTY_PATH) == 0
        assert acl_text(descriptor, b"", EMPTY_PATH, DEFAULT) == ""

        os.symlink(victim / "file", "file-link", dir_fd=descriptor)
        os.symlink(victim, "directory-link", dir_fd=descriptor)
        acl = library.acl_from_text(other.encode("ascii"))
        assert acl
        try:
            operations = [
                ("get", library.acl_get_file_at, (descriptor, b"file-link", NOFOLLOW, ACCESS), None),
                ("set", library.acl_set_file_at, (descriptor, b"file-link", NOFOLLOW, ACCESS, acl), -1),
                ("extended", library.acl_extended_file_at, (descriptor, b"file-link", NOFOLLOW), -1),
                ("delete-default", library.acl_delete_def_file_at, (descriptor, b"directory-link", NOFOLLOW), -1),
            ]
            for label, function, arguments, failure in operations:
                ctypes.set_errno(0)
                result = function(*arguments)
                assert result == failure, (label, result, ctypes.get_errno())
                assert ctypes.get_errno() in (errno.EOPNOTSUPP, errno.ELOOP, errno.EPERM), (label, ctypes.get_errno())
            for label, function, arguments, failure in operations:
                bad = list(arguments)
                bad[2] = BAD_FLAG
                ctypes.set_errno(0)
                assert function(*bad) == failure, label
                assert ctypes.get_errno() == errno.EINVAL, (label, ctypes.get_errno())
        finally:
            assert library.acl_free(acl) == 0
        assert acl_text(victim_descriptor, b"file", NOFOLLOW) == victim_file_before
        assert acl_text(victim_descriptor, b"", EMPTY_PATH, DEFAULT) == victim_default_before
        return {"trusted_directory_after_ancestor_replacement": "passed", "descriptor_empty_path": "passed", "final_symlink_refusal": 4, "invalid_flag_refusal": 4, "unintended_target_unchanged": "passed", "legacy_path_getter_still_follows_replaced_ancestor": True}
    finally:
        os.close(descriptor)
        os.close(victim_descriptor)


def compatibility_tests(root: Path) -> dict:
    source = root / "tree"
    source.mkdir()
    (source / "folder").mkdir()
    file = source / "folder" / "café.txt"
    file.write_bytes(b"Preserve contents, modes and access ACLs.\n")
    os.link(file, source / "hardlink")
    (source / "symlink").symlink_to("folder/café.txt")
    (source / "directory-link").symlink_to("folder", target_is_directory=True)
    with (source / "sparse").open("wb") as handle:
        handle.seek(1024 * 1024)
        handle.write(b"end")
    descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        set_acl(descriptor, b"folder/caf\xc3\xa9.txt", NOFOLLOW, "u::rw-,u:12345:r--,g::---,m::r--,o::---")
        set_acl(descriptor, b"folder", NOFOLLOW, "u::rwx,u:12345:r-x,g::---,m::r-x,o::---", DEFAULT)
    finally:
        os.close(descriptor)
    copied = root / "copy"
    restored = root / "restore"
    restored.mkdir()
    run("cp", "-a", str(source), str(copied))
    run("tar", "--acls", "--xattrs", "--sparse", "-cf", str(root / "archive.tar"), "-C", str(source), ".")
    run("tar", "--acls", "--xattrs", "--no-same-owner", "-xf", str(root / "archive.tar"), "-C", str(restored))
    for destination in (copied, restored):
        for original in source.rglob("*"):
            relative = original.relative_to(source)
            replacement = destination / relative
            assert stat.S_IMODE(original.lstat().st_mode) == stat.S_IMODE(replacement.lstat().st_mode), relative
            if original.is_symlink():
                assert os.readlink(original) == os.readlink(replacement), relative
                continue
            if original.is_file():
                assert original.read_bytes() == replacement.read_bytes(), relative
            first = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
            second = os.open(replacement, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                assert acl_text(first, b"", EMPTY_PATH) == acl_text(second, b"", EMPTY_PATH), relative
                if original.is_dir():
                    assert acl_text(first, b"", EMPTY_PATH, DEFAULT) == acl_text(second, b"", EMPTY_PATH, DEFAULT), relative
            finally:
                os.close(first)
                os.close(second)
        assert (destination / "hardlink").stat().st_ino == (destination / "folder" / "café.txt").stat().st_ino
    timeout = subprocess.run(["timeout", "--signal=TERM", "--kill-after=0.5", "0.1", "sleep", "10"], capture_output=True, timeout=3)
    assert timeout.returncode == 124
    return {"coreutils_copy": "passed", "tar_acl_archive_roundtrip": "passed", "contents_modes_symlinks_hardlinks_sparse_access_and_default_acls": "passed", "gnu_timeout_deadline_exit": 124}


def main() -> None:
    assert os.getuid() != 0
    versions = subprocess.run(["dpkg-query", "-W", "libacl1", "tar", "coreutils", "libc6"], capture_output=True, text=True, check=True).stdout
    assert "2.4.0-1" in versions and "1.35+dfsg-6" in versions
    with tempfile.TemporaryDirectory(prefix="acl-verify-") as directory:
        root = Path(directory)
        results = {"uid": os.getuid(), "versions": versions, "safe_api": descriptor_tests(root), "retained_cli_compatibility": compatibility_tests(root)}
    print(json.dumps(results))


if __name__ == "__main__":
    main()
