"""Kernel-quota substitute for isolated command/protocol unit tests only."""
from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

from bananawiki.ops.project_quota import QuotaError, QuotaWitness


class FakeProjectQuota:
    def __init__(self, instances_dir: Path):
        self.instances_dir = Path(instances_dir)
        self.entries: dict[str, QuotaWitness] = {}
        self.failure: str | None = None

    def prepare(self, tenant, *, byte_limit, inode_limit, repair=False, expected_descriptor=None):
        if self.failure:
            raise QuotaError(self.failure)
        info = (self.instances_dir / tenant).stat()
        if expected_descriptor is not None:
            if type(expected_descriptor) is not int or expected_descriptor < 0:
                raise QuotaError("trusted pre-scan descriptor is invalid")
            expected = os.fstat(expected_descriptor)
            if (not stat.S_ISDIR(expected.st_mode)
                    or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
                raise QuotaError("tenant storage differs from trusted pre-scan descriptor")
        inode = info.st_ino
        old = self.entries.get(tenant)
        if old and old.root_inode != inode and not repair:
            raise QuotaError("tenant identity changed")
        if old and old.root_inode == inode:
            witness = replace(old, byte_limit=byte_limit, inode_limit=inode_limit)
        else:
            witness = QuotaWitness(project_id=1_000_000 + len(self.entries), byte_limit=byte_limit,
                                   inode_limit=inode_limit, filesystem="test-xfs-uuid", root_inode=inode,
                                   root_generation=1, storage_reserve_bytes=256 * 1024 ** 2)
        self.entries[tenant] = witness
        return witness

    def verify(self, tenant, *, byte_limit=None, inode_limit=None):
        if self.failure:
            raise QuotaError(self.failure)
        witness = self.entries.get(tenant)
        if witness is None or witness.root_inode != (self.instances_dir / tenant).stat().st_ino:
            raise QuotaError("tenant has no verified quota")
        if byte_limit is not None and byte_limit != witness.byte_limit:
            raise QuotaError("byte quota drift")
        if inode_limit is not None and inode_limit != witness.inode_limit:
            raise QuotaError("inode quota drift")
        return witness

    def verify_descriptor(self, descriptor, witness):
        if self.failure or os.fstat(descriptor).st_ino != witness["root_inode"]:
            raise QuotaError(self.failure or "descriptor identity changed")
