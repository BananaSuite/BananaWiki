"""The policy for new passwords (bananawiki.core.passwords.check_new_password)."""

from __future__ import annotations

from importlib import resources

import pytest

from bananawiki.core import passwords


def test_the_common_password_list_ships_with_the_package():
    data = resources.files("bananawiki.core").joinpath("data/common-passwords.txt")
    assert data.is_file()
    assert resources.files("bananawiki.core").joinpath("data/common-passwords.LICENSE").is_file()
    common = passwords.common_passwords()
    assert 9000 <= len(common) <= 20000
    assert {"password", "password123", "qwertyuiop", "iloveyou"} <= common
    # Shorter entries would never be reached: the minimum length refuses them first.
    assert min(map(len, common)) >= passwords.MIN_LENGTH
    assert all(word == word.lower() and not word.startswith("#") for word in common)


@pytest.mark.parametrize("password", ["password123", "PASSWORD123", "Qwertyuiop", "  iloveyou1  "])
def test_common_passwords_are_refused(password):
    assert passwords.check_new_password(password) == "auth.error.password_common"


@pytest.mark.parametrize(("password", "username", "email"), [
    ("bananafan", "BananaFan", None),
    ("my-name-is-bananafan", "bananafan", None),
    ("xx_BOB_xx_2026", "bob", None),
    ("mario.rossi-2026", "someone", "Mario.Rossi@example.com"),
    ("mario-in-the-wiki", "someone", "mario+wiki@example.com"),
])
def test_passwords_with_the_username_or_the_address_are_refused(password, username, email):
    assert passwords.check_new_password(password, username=username, email=email) == "auth.error.password_personal"


@pytest.mark.parametrize(("password", "username", "email"), [
    ("correct horse battery", "bob", None),
    ("always learning more", "someone", "al@example.com"),  # two letters are too short to look for
    ("a quiet violet harbour", "bob", "not-an-address"),
    ("a quiet violet harbour", None, None),
])
def test_good_passwords_pass(password, username, email):
    assert passwords.check_new_password(password, username=username, email=email) is None


def test_length_rules_come_first():
    assert passwords.check_new_password("short") == "auth.error.password_too_short"
    assert passwords.check_new_password("") == "auth.error.password_too_short"
    assert passwords.check_new_password(None) == "auth.error.password_too_short"
    assert passwords.check_new_password("x" * (passwords.MAX_LENGTH + 1)) == "auth.error.password_too_long"


def test_validate_password_keeps_checking_the_length_only():
    """Other callers (the hosted wiki task runner) keep the length rule; the policy is for wiki accounts."""
    assert passwords.validate_password("password123") is None
    assert passwords.validate_password("short") == "auth.error.password_too_short"
