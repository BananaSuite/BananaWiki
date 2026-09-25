"""
Tests for random ID generation functionality.

Tests the generate_random_id() function that will be used for
migrating entities from sequential INTEGER IDs to random TEXT IDs.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


def test_generate_random_id_default_length():
    """Test that generate_random_id() generates 12-character IDs by default."""
    random_id = db.generate_random_id()
    assert len(random_id) == 12
    assert random_id.isalnum()
    assert random_id.islower()


def test_generate_random_id_custom_length():
    """Test that generate_random_id() accepts custom length parameter."""
    random_id = db.generate_random_id(length=16)
    assert len(random_id) == 16
    assert random_id.isalnum()
    assert random_id.islower()


def test_generate_random_id_uniqueness():
    """Test that generate_random_id() generates unique IDs."""
    ids = set()
    # Generate 1000 IDs and verify no collisions
    for _ in range(1000):
        random_id = db.generate_random_id()
        assert random_id not in ids, "Duplicate ID generated!"
        ids.add(random_id)


def test_generate_random_id_character_set():
    """Test that generated IDs only contain lowercase letters and digits."""
    import string
    allowed_chars = set(string.ascii_lowercase + string.digits)

    for _ in range(100):
        random_id = db.generate_random_id()
        id_chars = set(random_id)
        assert id_chars.issubset(allowed_chars), f"ID contains invalid characters: {random_id}"


def test_generate_random_id_no_uppercase():
    """Test that generated IDs never contain uppercase letters."""
    for _ in range(100):
        random_id = db.generate_random_id()
        assert random_id == random_id.lower(), f"ID contains uppercase: {random_id}"


def test_generate_random_id_cryptographically_secure():
    """Test that IDs are generated using secrets module (cryptographically secure)."""
    # This is a meta-test - verify the function uses secrets module
    import inspect
    source = inspect.getsource(db.generate_random_id)
    assert 'secrets.choice' in source, "Function should use secrets.choice for security"


def test_generate_random_id_min_length():
    """Test generating IDs with minimum length."""
    random_id = db.generate_random_id(length=1)
    assert len(random_id) == 1


def test_generate_random_id_large_length():
    """Test generating IDs with large length."""
    random_id = db.generate_random_id(length=128)
    assert len(random_id) == 128


def test_generate_random_id_distribution():
    """Test that generated IDs have good character distribution."""
    # Generate many IDs and check that all allowed characters appear
    import string
    allowed_chars = set(string.ascii_lowercase + string.digits)
    seen_chars = set()

    # Generate enough IDs to likely see all characters
    for _ in range(500):
        random_id = db.generate_random_id(length=20)
        seen_chars.update(random_id)

    # Should see most characters (allow some to be missing due to randomness)
    coverage = len(seen_chars) / len(allowed_chars)
    assert coverage > 0.8, f"Poor character distribution: only {coverage:.1%} coverage"
