"""Root-agent XFS project quotas, verified before a tenant can write.

Only Linux x86_64 with effective project accounting *and* enforcement is
supported. The trusted caller serializes tenant lifecycle operations and must
stop every writer before ``prepare(repair=True)``. New/copy/restore destinations
must be prepared before copying data. Tenant containers must retain the pinned
quota-setter-denying seccomp policy; root-only verification then relies on that
kernel inheritance invariant instead of walking a running, mutable tree.

ABI definitions follow Linux's dqblk_xfs.h, fs.h and xfs_fs.h UAPI. No device
paths, external quota utilities, mount changes, or tenant-owned markers are used.
Primary ABI references:
https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/include/uapi/linux/dqblk_xfs.h
https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/include/uapi/linux/fs.h
https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/fs/xfs/libxfs/xfs_fs.h
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import os
import platform
import re
import secrets
import sqlite3
import stat
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

_TENANT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}(?:__apex)?\Z")
_MAX_ID = (1 << 32) - 1
_MAX_LIMIT = (1 << 63) - 1
_PROJINHERIT = 0x200
_REALTIME = 0x1 | 0x100  # realtime file / realtime inheritance
_GETATTR = 0x801C581F
_SETATTR = 0x401C5820
_BULKSTAT = 0x8040587F
_OPEN = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_ENTRIES = 200_000
_MAX_DEPTH = 128


class QuotaError(RuntimeError):
    """An effective, trusted tenant quota could not be established."""


@dataclass(frozen=True)
class QuotaWitness:
    project_id: int
    byte_limit: int
    inode_limit: int
    filesystem: str
    root_inode: int
    root_generation: int
    storage_reserve_bytes: int
    accounting: bool = True
    enforcement: bool = True
    inheritance: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class _StatFS(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_long), ("bsize", ctypes.c_long),
                ("blocks", ctypes.c_ulong), ("bfree", ctypes.c_ulong),
                ("bavail", ctypes.c_ulong), ("files", ctypes.c_ulong),
                ("ffree", ctypes.c_ulong), ("fsid", ctypes.c_int * 2),
                ("namelen", ctypes.c_long), ("frsize", ctypes.c_long),
                ("flags", ctypes.c_long), ("spare", ctypes.c_long * 4)]


class _DiskQuota(ctypes.Structure):
    _fields_ = [("version", ctypes.c_int8), ("flags", ctypes.c_int8),
                ("fieldmask", ctypes.c_uint16), ("id", ctypes.c_uint32),
                *[(name, ctypes.c_uint64) for name in
                  ("bhard", "bsoft", "ihard", "isoft", "bcount", "icount")],
                ("itimer", ctypes.c_int32), ("btimer", ctypes.c_int32),
                ("iwarns", ctypes.c_uint16), ("bwarns", ctypes.c_uint16),
                ("timerhi", ctypes.c_int8 * 4),
                *[(name, ctypes.c_uint64) for name in ("rtbhard", "rtbsoft", "rtbcount")],
                ("rtbtimer", ctypes.c_int32), ("rtbwarns", ctypes.c_uint16),
                ("padding3", ctypes.c_int16), ("padding4", ctypes.c_char * 8)]


class _QFile(ctypes.Structure):
    _fields_ = [("ino", ctypes.c_uint64), ("blocks", ctypes.c_uint64),
                ("extents", ctypes.c_uint32), ("pad", ctypes.c_uint32)]


class _QuotaStat(ctypes.Structure):
    _fields_ = [("version", ctypes.c_int8), ("pad1", ctypes.c_uint8),
                ("flags", ctypes.c_uint16), ("incore", ctypes.c_uint32),
                ("user", _QFile), ("group", _QFile), ("project", _QFile),
                ("timers", ctypes.c_int32 * 3), ("warns", ctypes.c_uint16 * 4),
                ("pad4", ctypes.c_uint32), ("pad2", ctypes.c_uint64 * 7)]


class _BulkHeader(ctypes.Structure):
    _fields_ = [("ino", ctypes.c_uint64), ("flags", ctypes.c_uint32),
                ("icount", ctypes.c_uint32), ("ocount", ctypes.c_uint32),
                ("agno", ctypes.c_uint32), ("reserved", ctypes.c_uint64 * 5)]


class _BulkStat(ctypes.Structure):
    _fields_ = [("ino", ctypes.c_uint64), ("size", ctypes.c_uint64),
                ("blocks", ctypes.c_uint64), ("xflags", ctypes.c_uint64),
                ("times", ctypes.c_int64 * 4),
                ("generation", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32), ("project", ctypes.c_uint32),
                ("nanoseconds", ctypes.c_uint32 * 4),
                ("hints", ctypes.c_uint32 * 4), ("nlink", ctypes.c_uint32),
                ("extents", ctypes.c_uint32), ("aextents", ctypes.c_uint32),
                ("version", ctypes.c_uint16), ("forkoff", ctypes.c_uint16),
                ("sick", ctypes.c_uint16), ("checked", ctypes.c_uint16),
                ("mode", ctypes.c_uint16), ("pad2", ctypes.c_uint16),
                ("extents64", ctypes.c_uint64), ("pad", ctypes.c_uint64 * 6)]


class _BulkRequest(ctypes.Structure):
    _fields_ = [("header", _BulkHeader), ("entry", _BulkStat)]


def _require_root() -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() != 0:
        raise QuotaError("Project quotas require the trusted Linux x86_64 root agent")


class _Kernel:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long

    def _quotactl(self, fd: int, command: int, project: int, output) -> None:
        # quotactl_fd is syscall 443 on the explicitly supported x86_64 ABI.
        result = self.libc.syscall(ctypes.c_long(443), ctypes.c_int(fd),
                                   ctypes.c_uint(((ord("X") << 8) + command) << 8 | 2),
                                   ctypes.c_uint(project), ctypes.byref(output))
        if result == -1:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))

    def filesystem(self, fd: int) -> str:
        result = _StatFS()
        if self.libc.fstatfs(ctypes.c_int(fd), ctypes.byref(result)) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        if result.kind != 0x58465342:
            raise QuotaError("Tenant storage must be an XFS filesystem with enforced project quotas")
        # XFS statfs f_fsid is the device number, which may change after reboot.
        # The on-disk UUID is the durable allocation namespace instead.
        geometry = bytearray(112)
        fcntl.ioctl(fd, 0x80705864, geometry, True)  # XFS_IOC_FSGEOMETRY_V1
        inode_size = struct.unpack_from("=I", geometry, 24)[0]
        realtime_blocks = struct.unpack_from("=Q", geometry, 40)[0]
        uuid = bytes(geometry[64:80])
        flags = struct.unpack_from("=I", geometry, 92)[0]
        if not any(uuid) or not flags & (1 << 11) or inode_size > 4096 or realtime_blocks:
            raise QuotaError("XFS storage requires UUID, 32-bit project IDs and ordinary non-realtime geometry")
        return "xfs:" + uuid.hex()

    def enforced(self, fd: int) -> None:
        result = _QuotaStat()
        result.version = 1
        self._quotactl(fd, 8, 0, result)
        if result.version != 1 or result.flags & 0x30 != 0x30:
            raise QuotaError("XFS project quota accounting and enforcement must both be enabled")

    def inode(self, fd: int, inode: int) -> _BulkStat:
        request = _BulkRequest()
        request.header.ino = inode
        request.header.icount = 1
        fcntl.ioctl(fd, _BULKSTAT, request, True)
        if request.header.ocount != 1 or request.entry.ino != inode or request.entry.version != 5:
            raise QuotaError("XFS inode identity could not be verified")
        return request.entry

    def attrs(self, fd: int) -> tuple[int, int]:
        output = bytearray(28)
        fcntl.ioctl(fd, _GETATTR, output, True)
        flags, _, _, project, _ = struct.unpack("=5I8x", output)
        return flags, project

    def assign(self, fd: int, project: int, directory: bool) -> None:
        output = bytearray(28)
        fcntl.ioctl(fd, _GETATTR, output, True)
        flags, extent, nextents, _, cowextent = struct.unpack("=5I8x", output)
        if flags & _REALTIME:
            raise QuotaError("Realtime XFS tenant storage is unsupported")
        if directory:
            flags |= _PROJINHERIT
        output[:] = struct.pack("=5I8x", flags, extent, nextents, project, cowextent)
        fcntl.ioctl(fd, _SETATTR, output, True)
        read_flags, read_project = self.attrs(fd)
        if read_project != project or directory and not read_flags & _PROJINHERIT:
            raise QuotaError("XFS project assignment or inheritance readback failed")

    def occupied(self, fd: int, project: int) -> bool:
        result = _DiskQuota()
        result.version = 1
        try:
            self._quotactl(fd, 9, project, result)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH, errno.ENODATA):
                return False
            raise
        if result.version != 1 or not result.flags & 2 or result.id < project:
            raise QuotaError("XFS project allocation lookup returned an unexpected identity")
        return result.id == project

    def limits(self, fd: int, project: int) -> _DiskQuota:
        result = _DiskQuota()
        result.version = 1
        self._quotactl(fd, 3, project, result)
        if result.version != 1 or result.id != project or not result.flags & 2:
            raise QuotaError("XFS project quota readback has an unexpected identity")
        return result

    def set_limits(self, fd: int, project: int, byte_limit: int, inode_limit: int) -> None:
        value = _DiskQuota()
        value.version, value.flags, value.id = 1, 2, project
        value.fieldmask = 0x3F  # Clear soft/realtime limits; realtime attributes are refused.
        value.bhard, value.ihard = byte_limit // 512, inode_limit
        self._quotactl(fd, 4, project, value)


def _mount_id(fd: int) -> int:
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as source:
        for line in source:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise QuotaError("Storage mount identity could not be verified")


class ProjectQuota:
    """Durable, non-reused allocations on a dedicated installation XFS volume.

    ``state_dir`` must be a root-owned private directory outside tenant storage.
    A deleted root's allocation remains reserved. A replacement root gets a new
    allocation; a rename retains the same filesystem/inode/generation identity.
    Existing data requires explicit stopped-only repair. Symlink targets are
    never followed, and external hardlinks, special files and submounts fail shut.
    """

    def __init__(self, instances_dir: Path, state_dir: Path, *, max_bytes: int,
                 max_inodes: int, project_start: int = 1_000_000,
                 storage_reserve_bytes: int = 256 * 1024 * 1024):
        _require_root()
        self.max_bytes = self._positive(max_bytes, "Host byte ceiling")
        self.max_inodes = self._positive(max_inodes, "Host inode ceiling")
        if self.max_bytes % 512:
            raise QuotaError("Host byte ceiling must be a multiple of 512")
        self.project_start = self._positive(project_start, "Project ID start", _MAX_ID)
        self.storage_reserve_bytes = self._positive(storage_reserve_bytes, "Host storage reserve")
        self.instances_dir = Path(instances_dir).absolute()
        self.state_dir = Path(state_dir).absolute()
        if not self.state_dir.name or ".." in self.state_dir.parts or ".." in self.instances_dir.parts:
            raise QuotaError("Quota storage paths must be explicit normalized directory paths")
        if self.state_dir == self.instances_dir or self.instances_dir in self.state_dir.parents:
            raise QuotaError("Quota allocation state must be outside tenant storage")
        self._kernel = _Kernel()

    @staticmethod
    def _positive(value, name: str, maximum: int = _MAX_LIMIT) -> int:
        if type(value) is not int or not 0 < value <= maximum:
            raise QuotaError(f"{name} must be a finite positive integer")
        return value

    def _limits(self, byte_limit: int, inode_limit: int) -> None:
        self._positive(byte_limit, "Tenant byte limit", self.max_bytes)
        self._positive(inode_limit, "Tenant inode limit", self.max_inodes)
        if byte_limit % 512:
            raise QuotaError("Tenant byte limit must be a multiple of 512")

    @contextlib.contextmanager
    def _root(self, tenant: str, *, expected_descriptor: int | None = None):
        if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
            raise QuotaError("Invalid tenant storage identity")
        if expected_descriptor is not None and (type(expected_descriptor) is not int
                                                or expected_descriptor < 0):
            raise QuotaError("Trusted pre-scan storage descriptor must be an open directory FD")
        expected = os.fstat(expected_descriptor) if expected_descriptor is not None else None
        base = os.open(self.instances_dir, _OPEN | os.O_DIRECTORY)
        try:
            root = os.open(tenant, _OPEN | os.O_DIRECTORY, dir_fd=base)
            try:
                info = os.fstat(root)
                if info.st_uid == 0 or info.st_gid == 0:
                    raise QuotaError("Tenant storage must have an unprivileged owner and group")
                if expected is not None:
                    if (not stat.S_ISDIR(expected.st_mode)
                            or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
                        raise QuotaError("Tenant storage differs from the trusted pre-scan descriptor")
                filesystem = self._kernel.filesystem(root)
                self._kernel.enforced(root)
                identity = self._kernel.inode(root, info.st_ino)
                if identity.mode != info.st_mode or identity.uid != info.st_uid:
                    raise QuotaError("Tenant root inode changed during verification")
                yield root, filesystem, info, identity
                current = os.stat(tenant, dir_fd=base, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_mode) != (
                        info.st_dev, info.st_ino, info.st_mode):
                    raise QuotaError("Tenant storage root changed during quota verification")
            finally:
                os.close(root)
        finally:
            os.close(base)

    def _state_parent(self) -> int:
        """Pin trusted parents without following a symlink in the state path."""
        fd = os.open("/", _OPEN | os.O_DIRECTORY)
        try:
            for name in self.state_dir.parent.parts[1:]:
                child = os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                # A root-owned sticky /tmp cannot let another user rename a
                # root-owned child; useful for disposable acceptance fixtures.
                sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022 and not sticky:
                    raise QuotaError("Quota state parents must be trusted and not attacker-writable")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _trusted_state(self, directory: int | None = None) -> int:
        opened = directory is None
        if opened:
            parent = self._state_parent()
            try:
                try:
                    os.mkdir(self.state_dir.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                directory = os.open(self.state_dir.name, _OPEN | os.O_DIRECTORY, dir_fd=parent)
            finally:
                os.close(parent)
        try:
            info = os.fstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise QuotaError("Quota allocation state must be a private root-owned directory")
            names = {"allocations.sqlite3", "allocations.sqlite3-journal", "allocation.lock"}
            if set(os.listdir(directory)) - names:
                raise QuotaError("Quota state must be a dedicated allocation directory")
            # systemd shares StateDirectoryMode with Caddy's public routes
            # directory. Normalize only this securely pinned root-owned leaf.
            os.fchmod(directory, 0o700)
            for name in names:
                try:
                    value = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                        or value.st_mode & 0o077 or value.st_nlink != 1):
                    raise QuotaError("Quota registry files must be private root-owned regular files")
            return directory
        except BaseException:
            if opened:
                os.close(directory)
            raise

    @contextlib.contextmanager
    def _registry(self):
        directory = self._trusted_state()
        try:
            lock = os.open("allocation.lock", os.O_CREAT | _OPEN, 0o600, dir_fd=directory)
        except BaseException:
            os.close(directory)
            raise
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._trusted_state(directory)
            # SQLite and its journal resolve through a pinned private directory,
            # so a parent rename cannot redirect the database after validation.
            path = f"/proc/self/fd/{directory}/allocations.sqlite3"
            fresh = not os.path.exists(path)
            if fresh:
                fd = os.open("allocations.sqlite3", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                os.close(fd)
            connection = sqlite3.connect(path, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("CREATE TABLE IF NOT EXISTS allocations ("
                                   "filesystem TEXT NOT NULL, inode INTEGER NOT NULL, generation INTEGER NOT NULL, "
                                   "project INTEGER NOT NULL, tenant TEXT NOT NULL, bytes INTEGER NOT NULL, "
                                   "inodes INTEGER NOT NULL, ready INTEGER NOT NULL DEFAULT 0, "
                                   "PRIMARY KEY(filesystem,inode,generation), UNIQUE(filesystem,project))")
                connection.execute("CREATE TABLE IF NOT EXISTS counters (filesystem TEXT PRIMARY KEY, "
                                   "next_project INTEGER NOT NULL)")
                connection.commit()
                if fresh:
                    os.fsync(directory)
                yield connection
            finally:
                connection.close()
        finally:
            os.close(lock)
            os.close(directory)

    @staticmethod
    def _row(connection, filesystem, inode, generation):
        return connection.execute("SELECT * FROM allocations WHERE filesystem=? AND inode=? AND generation=?",
                                  (filesystem, inode, generation)).fetchone()

    def _allocate(self, connection, fd, filesystem, inode, generation, tenant, byte_limit, inode_limit):
        result = connection.execute("SELECT next_project FROM counters WHERE filesystem=?",
                                    (filesystem,)).fetchone()
        candidate = self.project_start if result is None else max(self.project_start, result[0])
        # IDs used by another installation/operator are skipped, never adopted.
        for _ in range(100_000):
            if candidate > _MAX_ID:
                raise QuotaError("XFS project quota ID range is exhausted")
            used = connection.execute("SELECT 1 FROM allocations WHERE filesystem=? AND project=?",
                                      (filesystem, candidate)).fetchone()
            if not used and not self._kernel.occupied(fd, candidate):
                break
            candidate += 1
        else:
            raise QuotaError("XFS project ID collision search exceeded its safe bound")
        # Capacity refusal must not create a durable, unusable reservation that
        # could deny service to already admitted tenants.
        self._capacity(connection, fd, filesystem, desired=(candidate, byte_limit, inode_limit))
        connection.execute("INSERT OR REPLACE INTO counters VALUES (?,?)", (filesystem, candidate + 1))
        connection.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,0)",
                           (filesystem, inode, generation, candidate, tenant, byte_limit, inode_limit))
        # Persist reservation BEFORE any quota/inode mutation. Failure never frees an ID.
        connection.commit()
        return self._row(connection, filesystem, inode, generation)

    def _capacity(self, connection, fd, filesystem, *, desired=None) -> None:
        """Admit reservations, rather than overcommitting individual hard limits.

        A static ceiling on the sum of all hard limits prevents a tenant's
        concurrent delete/regrow cycle from creating apparent admission space.
        The dedicated-volume profile also budgets 4KiB per hard inode limit
        for inode/metadata growth. The free/unused check detects untracked
        operator usage; neither check provides a hierarchical portal quota.
        """
        outstanding = 0
        total_budget = 0
        found = False
        for row in connection.execute("SELECT * FROM allocations WHERE filesystem=?", (filesystem,)):
            byte_limit, inode_limit = row["bytes"], row["inodes"]
            self._limits(byte_limit, inode_limit)
            try:
                actual = self._kernel.limits(fd, row["project"])
                used_bytes, used_inodes = actual.bcount * 512, actual.icount
                if actual.rtbcount:
                    raise QuotaError("Realtime XFS tenant storage is unsupported")
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ESRCH):
                    raise
                if row["ready"]:
                    raise QuotaError("An allocated project quota record is missing") from exc
                used_bytes, used_inodes = 0, 0
                actual = None
            if desired is not None and row["project"] == desired[0]:
                byte_limit, inode_limit = desired[1:]
                found = True
            elif not used_bytes and not used_inodes and row["ready"]:
                # Ready projects become empty only after all charged inodes
                # (including the root) have been deleted. The ID stays reserved.
                continue
            elif row["ready"] and (actual.bhard * 512, actual.ihard) != (byte_limit, inode_limit):
                raise QuotaError("An allocated tenant's hard limits differ from its trusted reservation")
            elif not row["ready"] and actual is not None:
                # A failed limit reduction may have left the old, larger kernel
                # cap. Keep its unused budget reserved until stopped recovery.
                byte_limit = max(byte_limit, actual.bhard * 512)
                inode_limit = max(inode_limit, actual.ihard)
            outstanding += max(byte_limit - used_bytes, 0) + max(inode_limit - used_inodes, 0) * 4096
            total_budget += byte_limit + inode_limit * 4096
        if desired is not None and not found:
            outstanding += desired[1] + desired[2] * 4096
            total_budget += desired[1] + desired[2] * 4096
        # Free space and project usage are separate kernel snapshots. Deletion
        # between them can overstate remaining capacity, so admission must also
        # satisfy the usage-independent total hard-limit ceiling below.
        base = os.open(self.instances_dir, _OPEN | os.O_DIRECTORY)
        try:
            if self._kernel.filesystem(base) != filesystem:
                raise QuotaError("Tenant filesystem changed during capacity admission")
            flags, project = self._kernel.attrs(base)
            if project or flags & (_PROJINHERIT | _REALTIME):
                raise QuotaError("Instances parent must remain outside tenant project quotas")
            # statvfs on a PROJINHERIT root is clamped to *that project's*
            # limits. The unassigned instances parent reports physical space.
            available = os.fstatvfs(base)
            free_bytes = available.f_bavail * available.f_frsize
            total_bytes = available.f_blocks * available.f_frsize
        finally:
            os.close(base)
        if total_bytes < self.storage_reserve_bytes + total_budget:
            raise QuotaError("XFS total capacity cannot cover tenant hard-limit budgets and the host reserve")
        if free_bytes < self.storage_reserve_bytes + outstanding:
            raise QuotaError("XFS capacity cannot cover tenant hard-limit reservations and the host reserve")

    def _verify(self, fd, filesystem, info, identity, row, byte_limit=None, inode_limit=None):
        if row is None or row["ready"] != 1:
            raise QuotaError("Tenant project quota has not been prepared and verified")
        stored_bytes, stored_inodes = row["bytes"], row["inodes"]
        self._limits(stored_bytes, stored_inodes)
        if (byte_limit is not None and byte_limit != stored_bytes
                or inode_limit is not None and inode_limit != stored_inodes):
            raise QuotaError("Requested tenant limits differ from the trusted allocation")
        project = self._positive(row["project"], "Stored project ID", _MAX_ID)
        flags, actual_project = self._kernel.attrs(fd)
        if actual_project != project or identity.project != project or not flags & _PROJINHERIT:
            raise QuotaError("Tenant project identity or inheritance has been changed")
        if flags & _REALTIME:
            raise QuotaError("Realtime XFS tenant storage is unsupported")
        self._kernel.enforced(fd)
        limits = self._kernel.limits(fd, project)
        if (limits.bhard * 512, limits.ihard) != (stored_bytes, stored_inodes):
            raise QuotaError("Tenant hard byte or inode limits do not match the trusted allocation")
        if limits.rtbcount:
            raise QuotaError("Realtime XFS tenant storage is unsupported")
        if limits.bcount * 512 > stored_bytes or limits.icount > stored_inodes:
            raise QuotaError("Tenant usage exceeds its verified hard limits")
        return QuotaWitness(project, stored_bytes, stored_inodes, filesystem, info.st_ino,
                            identity.generation, self.storage_reserve_bytes)

    def verify(self, tenant: str, *, byte_limit: int | None = None,
               inode_limit: int | None = None) -> QuotaWitness:
        if byte_limit is not None:
            self._positive(byte_limit, "Tenant byte limit", self.max_bytes)
        if inode_limit is not None:
            self._positive(inode_limit, "Tenant inode limit", self.max_inodes)
        try:
            with self._root(tenant) as (fd, filesystem, info, identity), self._registry() as connection:
                row = self._row(connection, filesystem, info.st_ino, identity.generation)
                witness = self._verify(fd, filesystem, info, identity, row, byte_limit, inode_limit)
                self._capacity(connection, fd, filesystem)
                return witness
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise QuotaError(f"Tenant quota verification failed: {exc}") from exc

    def verify_descriptor(self, fd: int, witness: QuotaWitness | dict) -> None:
        """Check a trusted directory FD against its root-agent quota witness.

        The FD and witness must never be supplied by an IPC client. The caller
        holds the verified source FD while an inert Docker bootstrap starts,
        then checks a separately opened FD for the container's actual mounted
        data root before releasing any tenant workload. A Docker pathname bind
        alone does not preserve the source descriptor's inode identity.
        """
        try:
            if isinstance(witness, dict):
                witness = QuotaWitness(**witness)
            if not isinstance(witness, QuotaWitness) or type(fd) is not int or fd < 0:
                raise QuotaError("Invalid root-agent quota descriptor witness")
            self._limits(witness.byte_limit, witness.inode_limit)
            self._positive(witness.project_id, "Witness project ID", _MAX_ID)
            if (type(witness.root_inode) is not int or witness.root_inode <= 0
                    or type(witness.root_generation) is not int
                    or not 0 <= witness.root_generation <= _MAX_ID
                    or witness.storage_reserve_bytes != self.storage_reserve_bytes
                    or witness.accounting is not True or witness.enforcement is not True
                    or witness.inheritance is not True):
                raise QuotaError("Invalid root-agent quota descriptor identity")
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid == 0 or info.st_gid == 0:
                raise QuotaError("Pinned tenant storage must be an unprivileged-owned directory")
            filesystem = self._kernel.filesystem(fd)
            inode = self._kernel.inode(fd, info.st_ino)
            flags, project = self._kernel.attrs(fd)
            if (filesystem != witness.filesystem or info.st_ino != witness.root_inode
                    or inode.generation != witness.root_generation or inode.project != witness.project_id
                    or project != witness.project_id or not flags & _PROJINHERIT or flags & _REALTIME):
                raise QuotaError("Pinned storage does not match its verified project quota witness")
            self._kernel.enforced(fd)
            limits = self._kernel.limits(fd, project)
            if ((limits.bhard * 512, limits.ihard) != (witness.byte_limit, witness.inode_limit)
                    or limits.bcount * 512 > witness.byte_limit or limits.icount > witness.inode_limit
                    or limits.rtbcount):
                raise QuotaError("Pinned storage hard limits differ from its verified witness")
        except (OSError, TypeError, ValueError) as exc:
            raise QuotaError(f"Pinned tenant quota verification failed: {exc}") from exc

    def prepare(self, tenant: str, *, byte_limit: int, inode_limit: int,
                repair: bool = False, expected_descriptor: int | None = None) -> QuotaWitness:
        """Prepare only the directory proven stopped by the trusted root agent.

        ``expected_descriptor`` is an optional server-owned FD held across its
        actual-mounted container inventory. It must never come from IPC. Its
        pinned inode/device identity prevents pathname replacement from moving
        stopped-only repair onto a different directory after that inventory.
        """
        self._limits(byte_limit, inode_limit)
        if type(repair) is not bool:
            raise QuotaError("Stopped-only quota repair must be explicitly boolean")
        try:
            with self._root(tenant, expected_descriptor=expected_descriptor) as (
                    fd, filesystem, info, identity), self._registry() as connection:
                row = self._row(connection, filesystem, info.st_ino, identity.generation)
                if row is not None and row["ready"] == 1 and not repair:
                    witness = self._verify(fd, filesystem, info, identity, row)
                    if (byte_limit, inode_limit) != (row["bytes"], row["inodes"]):
                        actual = self._kernel.limits(fd, row["project"])
                        if actual.bcount * 512 > byte_limit or actual.icount > inode_limit:
                            raise QuotaError("Requested hard limits are below current tenant usage")
                        self._capacity(connection, fd, filesystem,
                                       desired=(row["project"], byte_limit, inode_limit))
                        connection.execute("UPDATE allocations SET bytes=?,inodes=?,ready=0 WHERE "
                                           "filesystem=? AND project=?",
                                           (byte_limit, inode_limit, filesystem, row["project"]))
                        connection.commit()
                        self._kernel.set_limits(fd, row["project"], byte_limit, inode_limit)
                        actual = self._kernel.limits(fd, row["project"])
                        if actual.bcount * 512 > byte_limit or actual.icount > inode_limit:
                            raise QuotaError("Tenant usage raced below-limit quota update; stopped repair required")
                        row = self._row(connection, filesystem, info.st_ino, identity.generation)
                        candidate = dict(row)
                        candidate["ready"] = 1
                        witness = self._verify(fd, filesystem, info, identity, candidate, byte_limit, inode_limit)
                        self._capacity(connection, fd, filesystem)
                        connection.execute("UPDATE allocations SET ready=1 WHERE filesystem=? AND project=?",
                                           (filesystem, row["project"]))
                        connection.commit()
                    self._capacity(connection, fd, filesystem)
                    return witness
                entries = self._scan(fd, info)
                if not repair and (entries or identity.project):
                    raise QuotaError("Existing tenant storage requires explicit stopped-only quota repair")
                if row is None:
                    row = self._allocate(connection, fd, filesystem, info.st_ino, identity.generation,
                                         tenant, byte_limit, inode_limit)
                project = self._positive(row["project"], "Stored project ID", _MAX_ID)
                self._capacity(connection, fd, filesystem, desired=(project, byte_limit, inode_limit))
                connection.execute("UPDATE allocations SET bytes=?,inodes=?,ready=0,tenant=? "
                                   "WHERE filesystem=? AND project=?",
                                   (byte_limit, inode_limit, tenant, filesystem, project))
                connection.commit()
                self._kernel.set_limits(fd, project, byte_limit, inode_limit)
                self._kernel.assign(fd, project, True)
                self._repair(fd, info, entries, project)
                for _parts, value in self._scan(fd, info):
                    inode = self._kernel.inode(fd, value.st_ino)
                    if inode.project != project or (stat.S_ISDIR(value.st_mode)
                                                    and not inode.xflags & _PROJINHERIT):
                        raise QuotaError("Existing tenant inode project readback failed")
                identity = self._kernel.inode(fd, info.st_ino)
                row = self._row(connection, filesystem, info.st_ino, identity.generation)
                candidate = dict(row)
                candidate["ready"] = 1
                witness = self._verify(fd, filesystem, info, identity, candidate, byte_limit, inode_limit)
                self._capacity(connection, fd, filesystem)
                connection.execute("UPDATE allocations SET ready=1 WHERE filesystem=? AND project=?",
                                   (filesystem, project))
                connection.commit()
                return witness
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise QuotaError(f"Tenant quota preparation failed: {exc}") from exc

    @staticmethod
    def _open_parts(root, parts):
        fd = os.dup(root)
        try:
            for part in parts:
                child = os.open(part, _OPEN | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _scan(self, root, root_info):
        entries, links = [], {}
        mount = _mount_id(root)
        pending = [()]
        while pending:
            parts = pending.pop()
            if len(parts) > _MAX_DEPTH:
                raise QuotaError("Tenant directory depth exceeds the safe repair bound")
            fd = self._open_parts(root, parts)
            try:
                if _mount_id(fd) != mount or os.fstat(fd).st_dev != root_info.st_dev:
                    raise QuotaError("Tenant storage contains a submount or cross-filesystem directory")
                for name in os.listdir(fd):
                    value = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if value.st_dev != root_info.st_dev:
                        raise QuotaError("Tenant storage contains a cross-filesystem inode")
                    path = (*parts, name)
                    if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)
                            or stat.S_ISLNK(value.st_mode)):
                        raise QuotaError("Tenant storage contains an unsupported special inode")
                    if stat.S_ISLNK(value.st_mode) and value.st_nlink != 1:
                        raise QuotaError("Multi-linked tenant symlinks require explicit operator migration")
                    entries.append((path, value))
                    if len(entries) > _MAX_ENTRIES:
                        raise QuotaError("Tenant storage exceeds the safe repair entry bound")
                    if stat.S_ISDIR(value.st_mode):
                        pending.append(path)
                    else:
                        key = (value.st_dev, value.st_ino)
                        count, total = links.get(key, (0, value.st_nlink))
                        if total != value.st_nlink:
                            raise QuotaError("Tenant hardlinks changed during repair validation")
                        links[key] = (count + 1, total)
            finally:
                os.close(fd)
        if any(count != total for count, total in links.values()):
            raise QuotaError("Tenant storage contains a hardlink outside its root")
        # Parents must inherit the project before a legacy symlink is recreated.
        return sorted(entries, key=lambda item: (len(item[0]), item[0]))

    def _repair(self, root, root_info, entries, project):
        mount = _mount_id(root)
        for parts, original in entries:
            parent = self._open_parts(root, parts[:-1])
            try:
                if _mount_id(parent) != mount or os.fstat(parent).st_dev != root_info.st_dev:
                    raise QuotaError("Tenant storage mount changed during repair")
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (
                        original.st_dev, original.st_ino, original.st_mode, original.st_nlink):
                    raise QuotaError("Tenant inode changed during stopped-only repair")
                if stat.S_ISLNK(current.st_mode):
                    inode = self._kernel.inode(root, current.st_ino)
                    if inode.project != project:
                        target = os.readlink(parts[-1], dir_fd=parent)
                        temporary = ".quota-link-" + secrets.token_hex(16)
                        os.symlink(target, temporary, dir_fd=parent)
                        try:
                            os.chown(temporary, current.st_uid, current.st_gid,
                                     dir_fd=parent, follow_symlinks=False)
                            new = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                            if self._kernel.inode(root, new.st_ino).project != project:
                                raise QuotaError("Recreated symlink did not inherit its tenant project")
                            old = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                            if (old.st_ino, old.st_dev, old.st_mode, old.st_nlink) != (
                                    current.st_ino, current.st_dev, current.st_mode, current.st_nlink):
                                raise QuotaError("Legacy symlink changed before atomic project repair")
                            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                        finally:
                            try:
                                os.unlink(temporary, dir_fd=parent)
                            except FileNotFoundError:
                                pass
                    continue
                child = os.open(parts[-1], _OPEN, dir_fd=parent)
                try:
                    value = os.fstat(child)
                    if (value.st_dev, value.st_ino, value.st_mode) != (
                            current.st_dev, current.st_ino, current.st_mode) or _mount_id(child) != mount:
                        raise QuotaError("Tenant inode changed while being opened for quota repair")
                    self._kernel.assign(child, project, stat.S_ISDIR(value.st_mode))
                finally:
                    os.close(child)
            finally:
                os.close(parent)
