"""The platform backup formats: ``BWBACKUP2``, written now, and ``BWBACKUP1``, written by 1.4 to 1.7.3.

``BWBACKUP1``: ``b"BWBACKUP1"`` + 12-byte nonce + AES-256-GCM ciphertext +
16-byte tag. One GCM nonce cannot encrypt more than 2^36 - 32 bytes (about
64 GiB), so a larger platform could not be backed up at all.

``BWBACKUP2`` (the STREAM construction): ``b"BWBACKUP2"`` + 32-byte random
salt, then the data in segments of :data:`SEGMENT` bytes (the last one
shorter, possibly empty), each encrypted on its own with AES-256-GCM and
followed by its 16-byte tag. The key of the file is derived from the backup
key and the salt with HKDF-SHA256; a segment's nonce is its number (11
bytes, big-endian) and a final-segment flag (1 byte), and the header is the
associated data of every segment. Segments cannot be reordered, dropped,
repeated or cut off at a segment boundary without a tag failing, and the
size of a backup has no practical limit.

:class:`EncryptedOutput` is a write-only stream, so the ZIP (portal secret
key and every tenant's data) is encrypted while it is produced and no
plaintext copy ever reaches the disk. Decryption authenticates the whole
file before its output is published.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import stat
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from filelock import FileLock

from . import RuntimeFailure

MAGIC = b"BWBACKUP2"
LEGACY_MAGIC = b"BWBACKUP1"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK = 1024 * 1024
SALT_SIZE = 32
SEGMENT = 1024 * 1024
# The segment number takes 11 bytes of the nonce: far more segments than any disk holds.
MAX_SEGMENTS = 2 ** 88
HKDF_INFO = b"BananaWiki platform backup BWBACKUP2"


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
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            descriptor = None
        except OSError:
            raise RuntimeFailure("not_configured", f"{path} must be a private regular file (mode 0600)") from None
        if descriptor is not None:
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_size != 32):
                    raise RuntimeFailure("not_configured", f"{path} must be an owned 32-byte regular file (mode 0600)")
                raw = handle.read(33)
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
            return handle.read(len(MAGIC)) in (MAGIC, LEGACY_MAGIC)
    except OSError:
        return False


def _file_key(key: bytes, salt: bytes) -> AESGCM:
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=HKDF_INFO).derive(key))


def _nonce(number: int, final: bool) -> bytes:
    if number >= MAX_SEGMENTS:
        raise ValueError("the backup has too many segments")
    return number.to_bytes(NONCE_SIZE - 1, "big") + (b"\x01" if final else b"\x00")


class EncryptedOutput:
    """A write-only ``BWBACKUP2`` stream that encrypts into *destination* (published by :meth:`finish`).

    It has no ``tell``/``seek``, so :mod:`zipfile` writes a streamed archive
    (data descriptors) into it.
    """

    def __init__(self, destination: Path, key: bytes):
        self.destination = Path(destination)
        descriptor, self._pending = tempfile.mkstemp(dir=self.destination.parent, prefix=".bwenc-")
        os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "wb")
        salt = os.urandom(SALT_SIZE)
        self._header = MAGIC + salt
        self._cipher = _file_key(key, salt)
        self._buffer = bytearray()
        self._segments = 0
        self._file.write(self._header)

    def _seal(self, data: bytes, final: bool) -> None:
        self._file.write(self._cipher.encrypt(_nonce(self._segments, final), data, self._header))
        self._segments += 1

    def write(self, data: bytes) -> int:
        self._buffer += data
        # A full segment is sealed once more data follows it: the final one is the one finish() seals.
        sealed = 0
        while len(self._buffer) - sealed > SEGMENT:
            self._seal(bytes(self._buffer[sealed:sealed + SEGMENT]), False)
            sealed += SEGMENT
        del self._buffer[:sealed]
        return len(data)

    def flush(self) -> None:
        self._file.flush()

    def finish(self) -> Path:
        try:
            self._seal(bytes(self._buffer), True)
            self._buffer.clear()
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()
        # Never overwrite a completed backup when two exports share a name.
        try:
            os.link(self._pending, self.destination)
        finally:
            Path(self._pending).unlink(missing_ok=True)
        return self.destination

    def abort(self) -> None:
        self._file.close()
        Path(self._pending).unlink(missing_ok=True)


def decrypt_file(source: Path, destination: Path, key: bytes) -> Path:
    """Decrypt *source* (either format) into *destination*, which appears only once every tag has verified."""
    with open(source, "rb") as handle:
        magic = handle.read(len(MAGIC))
        if magic not in (MAGIC, LEGACY_MAGIC):
            raise RuntimeFailure("archive_invalid", "not an encrypted BananaWiki backup")
        descriptor, pending = tempfile.mkstemp(dir=Path(destination).parent, prefix=".bwdec-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                if magic == MAGIC:
                    _decrypt_segments(handle, output, key)
                else:
                    _decrypt_legacy(handle, output, key)
            os.replace(pending, destination)
        finally:
            Path(pending).unlink(missing_ok=True)
    return Path(destination)


def _decrypt_segments(handle, output, key: bytes) -> None:
    salt = handle.read(SALT_SIZE)
    header = MAGIC + salt
    remaining = os.fstat(handle.fileno()).st_size - len(header)
    if len(salt) != SALT_SIZE or remaining < TAG_SIZE:
        raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
    cipher = _file_key(key, salt)
    number = 0
    while remaining:
        size = min(remaining, SEGMENT + TAG_SIZE)
        if size < TAG_SIZE:
            raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
        sealed = handle.read(size)
        if len(sealed) != size:
            raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
        remaining -= size
        try:
            # The segment that ends the file must be sealed as the final one: a backup cut off after a full
            # segment fails here.
            output.write(cipher.decrypt(_nonce(number, not remaining), sealed, header))
        except (InvalidTag, ValueError):
            raise RuntimeFailure("archive_invalid", "wrong key or damaged backup") from None
        number += 1


def _decrypt_legacy(handle, output, key: bytes) -> None:
    nonce = handle.read(NONCE_SIZE)
    header = len(LEGACY_MAGIC) + NONCE_SIZE
    size = os.fstat(handle.fileno()).st_size - header - TAG_SIZE
    if len(nonce) != NONCE_SIZE or size < 0:
        raise RuntimeFailure("archive_invalid", "the encrypted backup is truncated")
    handle.seek(-TAG_SIZE, os.SEEK_END)
    tag = handle.read(TAG_SIZE)
    handle.seek(header)
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
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
