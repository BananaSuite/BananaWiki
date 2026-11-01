"""Regression tests for the m4 findings of the full re-audit pass 7 (hosting portal and ops)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from bananawiki.ops import hosting_entrypoint as entrypoint
from tests.hosting.conftest import login, make_account, make_wiki, portal, query, web  # noqa: F401
from tests.hosting.test_cross_wiki_sign_in import platform  # noqa: F401

# MW4-1: repeated self-service sign-outs replace their older identical events


def test_mw4_1_repeated_sign_outs_keep_one_event_per_wiki(platform, make_account, make_wiki, login, query):  # noqa: F811
    from tests.hosting.test_cross_wiki_sign_in import _member_signed_in

    member, browser, _wiki = _member_signed_in(platform, make_account, login)
    other = make_wiki(make_account(), "other")
    query("INSERT INTO hosting_oauth_grants (account_id, instance_id, granted_at) VALUES (?, ?, '2026-01-01')",
          (member["id"], other["id"]))
    seen = []
    for path in ["/oauth/revoke", "/account/sessions/logout-all"] * 4:
        assert browser.post(path).status_code == 302
        seen += [row["id"] for row in query("SELECT id FROM hosting_oauth_signouts")]
    rows = query("SELECT instance_id, account_id, reason, id FROM hosting_oauth_signouts ORDER BY id")
    assert sorted(row["instance_id"] for row in rows) == sorted([platform["inst"]["id"], other["id"]])
    assert {row["account_id"] for row in rows} == {member["id"]}
    assert [row["id"] for row in rows] == sorted(set(seen))[-2:], "the newest events stay, with new ids"


def test_mw4_1_a_replaced_event_still_ends_every_earlier_session(platform, make_account, login, query):  # noqa: F811
    from tests.hosting.test_cross_wiki_sign_in import _member_signed_in, _run_signouts, _signed_in

    member, browser, first = _member_signed_in(platform, make_account, login)
    assert browser.post("/oauth/revoke").status_code == 302
    assert browser.post("/oauth/revoke").status_code == 302, "replaces the event the wiki has not fetched yet"
    assert _run_signouts(platform) == 1
    assert not _signed_in(platform, first)
    _other, _other_browser, second = _member_signed_in(platform, make_account, login, "member2")
    # The member signs in again after the wiki fetched the first event, then signs out again: the
    # replacing event gets an id beyond the wiki's cursor, so the newer session ends too.
    again = _sign_in_again(platform, browser)
    assert _signed_in(platform, again)
    assert browser.post("/oauth/revoke").status_code == 302
    assert len(query("SELECT 1 FROM hosting_oauth_signouts WHERE account_id = ?", (member["id"],))) == 1
    assert _run_signouts(platform) == 1
    assert not _signed_in(platform, again)
    assert _signed_in(platform, second), "another account's session is left alone"


def _sign_in_again(context, browser):
    from urllib.parse import urlsplit

    wiki = context["wiki"].test_client()
    authorize = urlsplit(wiki.get("/platform-oauth/login", base_url=context["url"]).headers["Location"])
    answer = browser.get(f"{authorize.path}?{authorize.query}")
    if answer.status_code == 200:
        answer = browser.post(f"{authorize.path}?{authorize.query}", data={"action": "allow"})
    allowed = urlsplit(answer.headers["Location"])
    wiki.get(f"{allowed.path}?{allowed.query}", base_url=context["url"])
    return wiki


# MW5-1: the hosting entrypoint's filter also refuses file_getattr/file_setattr and future syscalls


@pytest.mark.skipif(sys.platform != "linux" or entrypoint.platform.machine() != "x86_64",
                    reason="Managed hosting requires x86-64 Linux")
def test_mw5_1_file_setattr_is_refused_under_the_filter(tmp_path):
    path = tmp_path / "attr-target"
    path.write_text("x")
    script = r'''
import ctypes, json, os, sys
from bananawiki.ops.hosting_entrypoint import install_filter
library = ctypes.CDLL(None, use_errno=True)
library.syscall.restype = ctypes.c_long
attr = (ctypes.c_ubyte * 24)()
def call(number, *args):
    ctypes.set_errno(0)
    result = library.syscall(ctypes.c_long(number), *args)
    return 0 if result >= 0 else ctypes.get_errno()
def fileattr(number):
    return call(number, ctypes.c_long(-100), sys.argv[1].encode(), ctypes.byref(attr), ctypes.c_size_t(24),
                ctypes.c_uint(0))
before = {"getattr": fileattr(468)}
install_filter()
after = {"getattr": fileattr(468), "setattr": fileattr(469), "future": call(470, 0, 0, 0, 0, 0),
         "far": call(0x3fffffff, 0, 0, 0, 0, 0), "getpid": call(39)}
print(json.dumps({"before": before, "after": after}))
'''
    completed = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["after"]["getattr"] == 1 and result["after"]["setattr"] == 1, result
    assert result["after"]["future"] == 38 and result["after"]["far"] == 38, "unknown syscalls look absent"
    assert result["after"]["getpid"] == 0
