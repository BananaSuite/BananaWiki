"""Authenticator codes shared by the wiki and the hosting portal (bananawiki.core.totp)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, unquote

import pytest
from cryptography.fernet import Fernet

from bananawiki.core import totp

# RFC 6238, appendix B (SHA-1): the last six digits of the eight-digit reference codes.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(("moment", "code"), [
    (59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
    (2000000000, "279037"), (20000000000, "353130"),
])
def test_codes_match_the_rfc_6238_reference_values(moment, code):
    assert totp.totp_code(RFC_SECRET, moment) == code


def test_a_code_is_accepted_one_step_either_side_and_no_further():
    now = 1_700_000_000
    step = now // 30
    assert totp.matching_counter(RFC_SECRET, totp.totp_code(RFC_SECRET, now), when=now) == step
    assert totp.matching_counter(RFC_SECRET, totp.totp_code(RFC_SECRET, now - 30), when=now) == step - 1
    assert totp.matching_counter(RFC_SECRET, totp.totp_code(RFC_SECRET, now + 30), when=now) == step + 1
    assert totp.matching_counter(RFC_SECRET, totp.totp_code(RFC_SECRET, now + 90), when=now) is None
    for bad in ("", "12345", "1234567", "abcdef", "１２３４５６", None):
        assert totp.matching_counter(RFC_SECRET, bad, when=now) is None


def test_secrets_round_trip_and_fail_closed_with_another_key():
    secret = totp.generate_secret()
    assert len(secret) == 32 and set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    token = totp.encrypt_secret(totp.cipher("key one", "purpose"), secret)
    assert totp.decrypt_secret(totp.cipher("key one", "purpose"), token) == secret
    assert totp.decrypt_secret(totp.cipher("key two", "purpose"), token) == ""
    assert totp.decrypt_secret(totp.cipher("key one", "other purpose"), token) == ""
    assert totp.decrypt_secret(totp.cipher("key one", "purpose"), "not a token") == ""


def test_the_hosting_portal_keeps_its_1_4_keys():
    """Enrolled portal accounts keep working: same Fernet key and recovery digests as 1.4."""
    key = "hosting-secret-" + "x" * 32
    legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256((key + "\0hosting-mfa-v1").encode()).digest()))
    token = legacy.encrypt(RFC_SECRET.encode()).decode()
    assert totp.decrypt_secret(totp.cipher(key, "hosting-mfa-v1"), token) == RFC_SECRET
    expected = hmac.new(key.encode(), b"mfa-recovery-v1\0ABCDEF012345", hashlib.sha256).hexdigest()
    assert totp.recovery_hash(key, "abcd-ef01 2345", "mfa-recovery-v1") == expected


def test_recovery_codes_are_distinct_and_readable():
    codes = totp.generate_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODES == len(set(codes))
    assert all(len(code) == 14 and code.count("-") == 2 for code in codes)


def test_provisioning_link_names_the_issuer_and_account():
    uri = totp.provisioning_uri("My Wiki", "bob", RFC_SECRET)
    assert uri.startswith("otpauth://totp/My%20Wiki%3Abob?secret=" + RFC_SECRET)
    assert "issuer=My%20Wiki&" in uri and "digits=6" in uri and "period=30" in uri
    assert totp.qr_data_uri(uri).startswith("data:image/png;base64,")


def test_a_colon_in_a_name_does_not_split_the_label_elsewhere():
    """Authenticator apps split the label on its first colon (Key URI format), so neither the issuer nor the
    account may contain one, and the label's issuer must be the issuer parameter."""
    uri = totp.provisioning_uri("My Wiki: Team", "bob:1", RFC_SECRET)
    label, query = uri.removeprefix("otpauth://totp/").split("?")
    assert unquote(label).split(":") == ["My Wiki Team", "bob 1"]
    assert parse_qs(query)["issuer"] == ["My Wiki Team"] and parse_qs(query)["secret"] == [RFC_SECRET]
