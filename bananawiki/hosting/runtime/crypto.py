"""The ``BWBACKUP1`` platform backup format (1.4 ``hosting/backup_crypto.py``, still readable).

Layout: ``b"BWBACKUP1"`` + 12-byte nonce + AES-256-GCM ciphertext + 16-byte
tag. 1.4 first wrote the whole plaintext ZIP (portal secret key and every
tenant's data) to the default temporary directory and encrypted it
afterwards; :class:`EncryptedOutput` is a write-only stream, so the ZIP is
encrypted while it is produced and no plaintext copy ever reaches the disk.
Decryption authenticates the whole file before its output is published.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from filelock import FileLock

from . import RuntimeFailure

MAGIC = b"BWBACKUP1"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK = 1024 * 1024


def load_key(environ_value: str, key_path: str) -> bytes:
    """``HOSTING_BACKUP_ENCRYPTION_KEY`` (URL-safe base64) or the key file, created (0600) when missing."""
    value = environ_value.strip()
    if value:
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (binascii.Error, ValueError):
            raise RuntimeFailure("not_configured", "HOSTING_BACKUP_ENCRYPTION_KEY is not valid base64") from None
        if len(raw) != 32:
            raise RuntimeFailure("not_configured", "HOSTING_BACKUP_ENCRYPTION_KEY must decode to 32 bytes")
        return raw
    path = Path(key_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=20, mode=0o600):
        if path.is_symlink():
            raise RuntimeFailure("not_configured", f"{path} must not be a symbolic link")
        if path.exists():
            raw = path.read_bytes()
            if len(raw) != 32:
                raise RuntimeFailure("not_configured", f"{path} must contain exactly 32 bytes")
            return raw
        raw = secrets.token_bytes(32)
        descriptor, pending = tempfile.mkstemp(dir=path.parent, prefix=".backup_key_")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(pending, 0o600)
            os.replace(pending, path)
        finally:
            Path(pending).unlink(missing_ok=True)
        return raw


def is_encrypted(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


class EncryptedOutput:
    """A write-only stream that encrypts into *destination* (published by :meth:`finish`).

    It has no ``tell``/``seek``, so :mod:`zipfile` writes a streamed archive
    (data descriptors) into it.
    """

    def __init__(self, destination: Path, key: bytes):
        self.destination = Path(destination)
        descriptor, self._pending = tempfile.mkstemp(dir=self.destination.parent, prefix=".bwenc-")
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "wb")
        nonce = os.urandom(NONCE_SIZE)
        self._encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self._file.write(MAGIC + nonce)

    def write(self, data: bytes) -> int:
        self._file.write(self._encryptor.update(bytes(data)))
        return len(data)

    def flush(self) -> None:
        self._file.flush()

    def finish(self) -> Path:
        try:
            self._file.write(self._encryptor.finalize())
            self._file.write(self._encryptor.tag)
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()
        os.replace(self._pending, self.destination)
        return self.destination

    def abort(self) -> None:
        self._file.close()
        Path(self._pending).unlink(missing_ok=True)


def decrypt_file(source: Path, destination: Path, key: bytes) -> Path:
    """Decrypt *source* into *destination*, which appears only once the tag has verified."""
    with open(source, "rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise RuntimeFailure("archive_invalid", "not an encrypted BananaWiki backup")
        nonce = handle.read(NONCE_SIZE)
        header = len(MAGIC) + NONCE_SIZE
        size = os.fstat(handle.fileno()).st_size - header - TAG_SIZE
        if len(nonce) != NONCE_SIZE or size < 0:
            raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
        handle.seek(-TAG_SIZE, os.SEEK_END)
        tag = handle.read(TAG_SIZE)
        handle.seek(header)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        descriptor, pending = tempfile.mkstemp(dir=Path(destination).parent, prefix=".bwdec-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                remaining = size
                while remaining:
                    chunk = handle.read(min(remaining, CHUNK))
                    if not chunk:
                        raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
                    output.write(decryptor.update(chunk))
                    remaining -= len(chunk)
                try:
                    output.write(decryptor.finalize())
                except InvalidTag:
                    raise RuntimeFailure("archive_invalid", "wrong key or damaged backup") from None
            os.replace(pending, destination)
        finally:
            Path(pending).unlink(missing_ok=True)
    return Path(destination)
