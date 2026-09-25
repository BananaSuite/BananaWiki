"""Stream AES-256-GCM encrypted platform backups in the BWBACKUP1 format."""

import os
from pathlib import Path
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MAGIC = b"BWBACKUP1"
_NONCE_SIZE = 12
_TAG_SIZE = 16
_CHUNK_SIZE = 1024 * 1024


def _get_key():
    from . import config
    return config.load_or_generate_backup_key()


def is_encrypted_backup(path):
    try:
        with open(path, "rb") as source:
            return source.read(len(_MAGIC)) == _MAGIC
    except OSError:
        return False


def encrypt_backup(src_path, dst_path=None):
    """Encrypt to an atomic, owner-readable file without loading the ZIP in RAM."""
    dst_path = dst_path or str(src_path) + ".bwenc"
    nonce = os.urandom(_NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(_get_key()), modes.GCM(nonce)).encryptor()
    fd, temporary = tempfile.mkstemp(dir=Path(dst_path).parent, prefix=".bwenc_")
    try:
        with os.fdopen(fd, "wb") as target, open(src_path, "rb") as source:
            target.write(_MAGIC + nonce)
            while chunk := source.read(_CHUNK_SIZE):
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, dst_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(dst_path)


def decrypt_backup(src_path, dst_path):
    """Authenticate the complete archive before replacing the destination."""
    with open(src_path, "rb") as source:
        if source.read(len(_MAGIC)) != _MAGIC:
            raise ValueError("Not a valid encrypted backup.")
        nonce = source.read(_NONCE_SIZE)
        header_size = len(_MAGIC) + _NONCE_SIZE
        ciphertext_size = os.fstat(source.fileno()).st_size - header_size - _TAG_SIZE
        if len(nonce) != _NONCE_SIZE or ciphertext_size < 0:
            raise ValueError("The encrypted backup is truncated.")
        source.seek(-_TAG_SIZE, os.SEEK_END)
        tag = source.read(_TAG_SIZE)
        source.seek(header_size)
        decryptor = Cipher(algorithms.AES(_get_key()), modes.GCM(nonce, tag)).decryptor()
        fd, temporary = tempfile.mkstemp(dir=Path(dst_path).parent, prefix=".bwdec_")
        try:
            with os.fdopen(fd, "wb") as target:
                remaining = ciphertext_size
                while remaining:
                    chunk = source.read(min(remaining, _CHUNK_SIZE))
                    if not chunk:
                        raise ValueError("The encrypted backup is truncated.")
                    target.write(decryptor.update(chunk))
                    remaining -= len(chunk)
                target.write(decryptor.finalize())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, dst_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return str(dst_path)
