"""Cross-platform password hashing compatibility tests."""


def test_auto_password_hashing_uses_supported_method(monkeypatch):
    from helpers import _passwords

    monkeypatch.delenv("BW_PASSWORD_HASH_METHOD", raising=False)
    monkeypatch.delenv("HASH_METHOD", raising=False)

    password_hash = _passwords.generate_password_hash("portable-password")

    assert password_hash.startswith(("scrypt:", "pbkdf2:"))
    assert _passwords.check_password_hash(password_hash, "portable-password")
    assert not _passwords.check_password_hash(password_hash, "wrong-password")


def test_auto_password_hashing_falls_back_without_scrypt(monkeypatch):
    from helpers import _passwords

    monkeypatch.delenv("BW_PASSWORD_HASH_METHOD", raising=False)
    monkeypatch.delenv("HASH_METHOD", raising=False)
    monkeypatch.delattr(_passwords.hashlib, "scrypt", raising=False)
    _passwords.is_scrypt_supported.cache_clear()

    try:
        assert _passwords.get_password_hash_method() == "pbkdf2:sha256"
        password_hash = _passwords.generate_password_hash("portable-password")
        assert password_hash.startswith("pbkdf2:")
        assert _passwords.check_password_hash(password_hash, "portable-password")
    finally:
        _passwords.is_scrypt_supported.cache_clear()


def test_explicit_pbkdf2_alias_is_supported(monkeypatch):
    from helpers import _passwords

    monkeypatch.setenv("BW_PASSWORD_HASH_METHOD", "pbkdf2")

    password_hash = _passwords.generate_password_hash("portable-password")

    assert password_hash.startswith("pbkdf2:")
    assert _passwords.check_password_hash(password_hash, "portable-password")


def test_unsupported_hash_check_fails_closed(monkeypatch):
    from helpers import _passwords

    def _raise_unsupported(_pwhash, _password):
        raise ValueError("unsupported hash type scrypt")

    monkeypatch.setattr(
        _passwords,
        "_werkzeug_check_password_hash",
        _raise_unsupported,
    )

    assert not _passwords.check_password_hash("scrypt:32768:8:1$salt$hash", "pw")
