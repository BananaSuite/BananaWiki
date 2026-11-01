"""Portal pages: what gated accounts are offered, forms that show what is stored, wiki pages, tables on
phones, banners, form patterns, copy buttons and the stylesheet rules they rely on."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from bananawiki.core.timeutil import to_sql, utcnow
from bananawiki.hosting import accounts, auth
from bananawiki.hosting.db import connection_scope

from .hosting_support import BOOTSTRAP, PASSWORD, build_portal, portal_environ

PACKAGE = Path(__file__).resolve().parents[2] / "bananawiki"
HOSTING = PACKAGE / "hosting"


def _unreachable(portal, account_id: str, page: str) -> list[str]:
    """Links and form targets on *page* that the account's pending gate would send back to the gate."""
    targets = {("POST", path) for path in re.findall(r'<form[^>]*action="(/[^"#?]*)', page)}
    targets |= {("GET", path) for path in re.findall(r'<a [^>]*href="(/[^"#?]*)', page)}
    adapter = portal.url_map.bind("localhost")
    bounced = []
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        account = accounts.get(account_id)
        for method, path in sorted(targets):
            if path.startswith("/static/"):
                continue
            try:
                endpoint, _args = adapter.match(path, method=method)
            except (NotFound, MethodNotAllowed):
                continue
            if auth.pending_gate(account, portal.view_functions[endpoint]) is not None:
                bounced.append(f"{method} {path}")
    return bounced


# Accounts held on one page by a gate


@pytest.mark.parametrize("approval", ["pending", "denied"])
def test_the_account_page_of_an_account_waiting_for_approval_offers_nothing_that_bounces(
        portal, make_account, login, query, approval):
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    fields = {"approval_status": approval}
    if approval == "denied":
        fields["denied_at"] = to_sql(utcnow())
    account = make_account(**fields)
    client = portal.test_client()
    login(client, account)
    page = client.get("/account").get_data(as_text=True)
    assert not _unreachable(portal, account["id"], page)
    # The sections that would only bounce are left out; changing the password works.
    assert "/account/merge-request" not in page and "/account/export" not in page
    assert re.search(r'action="/(account|settings)/change-password"', page)
    changed = client.post("/account/change-password", data={
        "current_password": PASSWORD, "new_password": "another-pass-77", "confirm_new_password": "another-pass-77"})
    assert urlsplit(changed.headers["Location"]).path == "/account"


def test_a_denied_account_is_not_told_it_is_waiting(portal, make_account, login):
    account = make_account(approval_status="denied", denied_at=to_sql(utcnow()))
    client = portal.test_client()
    login(client, account)
    page = client.get("/account").get_data(as_text=True)
    assert "An administrator did not approve this account." in page
    assert "waiting for an administrator" not in page
    assert 'href="/activation-denied"' in page


def test_an_account_with_new_terms_to_accept_gets_no_token_form(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(
        tmp_path, HOSTING_TERMS_REQUIRED_VERSION=accounts.TERMS_VERSION))
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        account = accounts.create("terms-user", PASSWORD)
        db.update("accounts", {"terms_version": ""}, "id = ?", (account["id"],))
        db.execute("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    client = portal.test_client()
    client.post("/login", data={"username": "terms-user", "password": PASSWORD})
    page = client.get("/account").get_data(as_text=True)
    assert not _unreachable(portal, account["id"], page)
    assert 'action="/account/api-tokens"' not in page
    # Downloading the data is what the terms page itself points to.
    assert 'action="/account/export"' in page


@pytest.mark.parametrize("state", ["deletion", "email_flag"])
def test_gate_pages_offer_no_navigation_that_leads_back_to_them(portal, make_account, login, state):
    if state == "deletion":
        fields = {"pending_deletion": 1, "pending_deletion_at": to_sql(utcnow()), "pending_deletion_seconds": 86400}
    else:
        fields = {"email": "", "email_flagged_invalid": 1, "email_flagged_previous": "gone@example.org"}
    account = make_account(**fields)
    client = portal.test_client()
    landed = urlsplit(login(client, account).headers["Location"]).path
    page = client.get(landed).get_data(as_text=True)
    nav = page[page.find('<nav class="topbar__actions'):page.find("</nav>", page.find('<nav class="topbar__actions'))]
    assert 'href="/dashboard"' not in nav and 'href="/account"' not in nav
    assert not _unreachable(portal, account["id"], nav)


def test_replacing_a_flagged_address_needs_the_password(portal, make_account, login):
    account = make_account(email="", email_flagged_invalid=1, email_flagged_previous="gone@example.org")
    client = portal.test_client()
    login(client, account)
    page = client.get("/account/email-flagged").get_data(as_text=True)
    assert "<strong>gone@example.org</strong>" in page and 'autocomplete="current-password"' in page
    assert "Questions? Write to" in page
    client.post("/account/email-flagged", data={"email": "new@example.org"})
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        assert accounts.get(account["id"])["email_flagged_invalid"]
    client.post("/account/email-flagged", data={"email": "new@example.org", "current_password": PASSWORD})
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        assert not accounts.get(account["id"])["email_flagged_invalid"]


def test_an_administrator_blocked_by_two_step_sees_no_dead_links(portal, make_account, login, query):
    admin = make_account(admin=True)
    make_account(approval_status="pending")
    query("UPDATE hosting_settings SET admin_mfa_required = 1 WHERE id = 1")
    client = portal.test_client()
    login(client, admin)
    gate = client.get("/account/mfa").get_data(as_text=True)
    assert "Review now" not in gate and 'href="/admin"' not in gate
    query("UPDATE accounts SET totp_enabled = 1 WHERE id = ?", (admin["id"],))
    assert "Review now" in client.get("/dashboard").get_data(as_text=True)


# Sign-up and sign-in


def test_a_refused_sign_up_marks_the_field_at_fault_and_keeps_the_consent(tmp_path):
    app = build_portal(tmp_path, environ=portal_environ(tmp_path))
    client = app.test_client()
    form = {"username": "newperson", "password": "abcdefg1", "confirm_password": "abcdefg2", "accept_terms": "1",
            "bootstrap_token": BOOTSTRAP}
    page = client.post(f"/signup?bootstrap_token={BOOTSTRAP}", data=form).get_data(as_text=True)
    confirm = re.search(r'<input id="f-confirm_password"[^>]*>', page).group(0)
    assert 'aria-invalid="true"' in confirm and "f-confirm_password-error" in confirm
    assert 'class="field-error" id="f-confirm_password-error"' in page
    assert re.search(r'name="accept_terms" value="1" required checked', page)
    # The consent is one sentence with both links.
    assert re.search(r"I accept the <a [^>]*>terms of service</a> and have read the <a [^>]*>privacy policy</a>\.",
                     page)


def test_after_a_password_mismatch_typing_starts_at_the_first_field(tmp_path):
    app = build_portal(tmp_path, environ=portal_environ(tmp_path))
    client = app.test_client()
    form = {"username": "newperson", "password": "abcdefg1", "confirm_password": "abcdefg2", "accept_terms": "1",
            "bootstrap_token": BOOTSTRAP}
    page = client.post(f"/signup?bootstrap_token={BOOTSTRAP}", data=form).get_data(as_text=True)
    assert "autofocus" in re.search(r'<input id="f-password"[^>]*>', page).group(0)
    assert "autofocus" not in re.search(r'<input id="f-confirm_password"[^>]*>', page).group(0)
    # The message is under the field; the top of the page only points there.
    assert page.count("The passwords do not match.") == 1
    assert "Please correct the field marked below." in page


def test_the_two_step_code_accepts_recovery_codes_on_phones(web, make_account, query):
    account = make_account("twostep")
    query("UPDATE accounts SET totp_enabled = 1 WHERE id = ?", (account["id"],))
    web.post("/login", data={"username": "twostep", "password": PASSWORD})
    page = web.get("/login/mfa").get_data(as_text=True)
    code = re.search(r'<input id="f-code"[^>]*>', page).group(0)
    assert 'inputmode="numeric"' not in code and 'autocapitalize="characters"' in code
    assert 'href="/login"' in page


def test_signing_out_other_devices_keeps_this_one(portal, make_account, login):
    account = make_account()
    here, there = portal.test_client(), portal.test_client()
    login(here, account)
    page = here.get("/account").get_data(as_text=True)
    assert "/account/sessions/logout-all" not in page  # one device: nothing else to sign out
    login(there, account)
    page = here.get("/account").get_data(as_text=True)
    assert "/account/sessions/logout-all" in page
    done = here.post("/account/sessions/logout-all")
    assert urlsplit(done.headers["Location"]).path == "/account"
    assert here.get("/account").status_code == 200
    assert urlsplit(there.get("/account").headers["Location"]).path == "/login"


# Forms that show what is stored


def test_the_denied_accounts_timeout_shows_its_stored_value(portal, make_account, login, query):
    admin = make_account(admin=True)
    query("UPDATE hosting_settings SET hosting_activation_denied_timeout_seconds = ? WHERE id = 1",
          (2 * 86400 + 3 * 3600,))
    client = portal.test_client()
    login(client, admin)
    page = client.get("/admin/settings").get_data(as_text=True)
    assert re.search(r'name="act_timeout_days" value="2"', page)
    assert re.search(r'name="act_timeout_hours" value="3"', page)
    assert "<legend>Delete them after</legend>" in page
    # Saving the section unchanged keeps the timeout.
    client.post("/admin/settings", data={"action": "save_activation", "hosting_activation_denied_deletion_enabled": "1",
                                         "act_timeout_days": "2", "act_timeout_hours": "3", "act_timeout_minutes": "0"})
    stored = query("SELECT hosting_activation_denied_timeout_seconds AS t FROM hosting_settings WHERE id = 1",
                   one=True)
    assert stored["t"] == 2 * 86400 + 3 * 3600


def test_a_new_platform_keeps_denied_accounts_for_a_day_as_documented(portal, make_account, login, query):
    # The documented default is one day, not 0 ("delete at once").
    stored = query("SELECT hosting_activation_denied_timeout_seconds AS t FROM hosting_settings WHERE id = 1",
                   one=True)
    assert stored["t"] == 86400
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    page = client.get("/admin/settings").get_data(as_text=True)
    assert 'name="act_timeout_days" value="1"' in page and 'name="act_timeout_hours" value="0"' in page


def test_scheduling_a_deletion_asks_for_days_and_shows_the_moment(portal, make_account, login):
    admin, member = make_account(admin=True), make_account()
    client = portal.test_client()
    login(client, admin)
    page = client.get(f"/admin/accounts/{member['id']}").get_data(as_text=True)
    assert 'name="deletion_days"' in page and 'name="deletion_seconds"' not in page
    assert "<legend>Delete after</legend>" in page
    client.post(f"/admin/accounts/{member['id']}/schedule-deletion",
                data={"deletion_days": "2", "deletion_hours": "0", "deletion_minutes": "0", "deletion_reason": "Spam"})
    page = client.get(f"/admin/accounts/{member['id']}").get_data(as_text=True)
    assert re.search(r"<dd><span class=\"nowrap\">\d{4}-\d\d-\d\d \d\d:\d\d UTC</span> \(<span class=\"nowrap\">", page)
    assert "<dt>Deletion reason</dt><dd>Spam</dd>" in page


def test_merge_requests_say_what_they_need(portal, make_account, login):
    account = make_account()
    client = portal.test_client()
    login(client, account)
    page = client.get("/account/merge-request").get_data(as_text=True)
    assert 'id="f-target_username-hint">Username</p>' in page and "Reason (optional)" in page


def test_deleting_an_account_groups_the_choice_for_the_wikis(portal, make_account, login):
    admin, member = make_account(admin=True), make_account()
    client = portal.test_client()
    login(client, admin)
    page = client.get(f"/admin/accounts/{member['id']}/delete").get_data(as_text=True)
    assert "<legend>What happens to the wikis</legend>" in page
    assert 'data-enabled-by="instance_action=transfer"' in page


# Wikis


def test_an_expired_wiki_is_neither_counted_as_running_nor_offered_for_termination(
        portal, make_account, make_wiki, login, query):
    owner = make_account()
    make_wiki(owner, "live-one")
    old = make_wiki(owner, "old-one")
    query("UPDATE instances SET expires_at = ? WHERE id = ?", (to_sql(utcnow() - timedelta(days=1)), old["id"]))
    client = portal.test_client()
    login(client, owner)
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert "<dt>running</dt><dd>1</dd>" in dashboard
    page = client.get(f"/instances/{old['id']}").get_data(as_text=True)
    assert f"/instances/{old['id']}/terminate" not in page and f"/instances/{old['id']}/restart" not in page
    query("UPDATE hosting_settings SET allow_owner_delete_expired = 1 WHERE id = 1")
    assert f"/instances/{old['id']}/terminate" in client.get(f"/instances/{old['id']}").get_data(as_text=True)


def test_a_deleted_wiki_offers_its_data_not_a_password_reset(portal, make_account, make_wiki, login):
    owner = make_account(admin=True)
    inst = make_wiki(owner, "gone-wiki")
    client = portal.test_client()
    login(client, owner)
    client.post(f"/instances/{inst['id']}/terminate")
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    assert "Download your data" in page and "Locked out" not in page
    assert "Largest upload" not in page


def test_a_pending_outgoing_transfer_shows_on_the_dashboard(portal, make_account, make_wiki, login):
    giver = make_account()
    make_account("receiver")
    inst = make_wiki(giver, "handed-over")
    client = portal.test_client()
    login(client, giver)
    client.post(f"/instances/{inst['id']}/transfer", data={"username": "receiver"})
    assert "Transfer to receiver pending" in client.get("/dashboard").get_data(as_text=True)


def test_accept_and_decline_of_a_transfer_share_one_row(portal, make_account, make_wiki, login):
    giver, taker = make_account(), make_account()
    inst = make_wiki(giver, "shared-notes")
    giving, taking = portal.test_client(), portal.test_client()
    login(giving, giver)
    giving.post(f"/instances/{inst['id']}/transfer", data={"username": taker["username"]})
    login(taking, taker)
    page = taking.get("/dashboard").get_data(as_text=True)
    row = re.search(r'<div class="btn-row">\s*<button[^>]*>Accept</button>\s*<button[^>]*form="decline-transfer-'
                    r'[^"]+"[^>]*>Decline</button>', page)
    assert row, page[page.find("wants to give you"):][:1500]


def test_an_administrator_correcting_the_intended_use_promises_nothing(portal, make_account, make_wiki, login):
    admin, owner = make_account(admin=True), make_account()
    inst = make_wiki(owner, "someone-elses")
    client = portal.test_client()
    login(client, admin)
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    assert 'name="compliance_declared"' not in page and "What this wiki is for" in page
    client.post(f"/instances/{inst['id']}/use-case", data={"declared_use_case": "A club handbook for members."})
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        assert db.scalar("SELECT declared_use_case FROM instances WHERE id = ?", (inst["id"],)) == \
            "A club handbook for members."


def test_the_owner_declares_the_intended_use_with_the_linked_terms(portal, make_account, make_wiki, login):
    owner = make_account()
    inst = make_wiki(owner, "mine")
    client = portal.test_client()
    login(client, owner)
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    assert re.search(r'name="compliance_declared" value="1" required><span>I will use this wiki in line with the '
                     r'<a href="/terms">terms of service</a>\.', page)


# Analytics


def test_the_fake_runtime_reports_its_traffic_inside_the_period(portal, make_account, make_wiki, login):
    # Like a real wiki, the demo runtime reports every day of the window, ending today, so its traffic shows in
    # the chart and the table of any period.
    owner = make_account()
    inst = make_wiki(owner, "demo-traffic")
    client = portal.test_client()
    login(client, owner)
    today = utcnow().date().isoformat()
    for days in (7, 365):
        page = client.get(f"/instances/{inst['id']}/analytics?days={days}").get_data(as_text=True)
        assert f'title="{today}: 7"' in page
        assert f'<td class="date">{today}</td><td>10</td><td>7</td><td>1</td>' in page
        assert '<td class="date">2026-01-01</td>' not in page and "No page views in this period." not in page


def _summary(daily: list[dict]) -> dict:
    kinds = ("request", "page_view", "error")
    return {"window_days": len(daily), "totals": {kind: sum(row[kind] for row in daily) for kind in kinds},
            "daily": daily}


def test_the_analytics_table_lists_only_days_with_traffic(portal, runtime, make_account, make_wiki, login,
                                                          monkeypatch):
    owner = make_account()
    inst = make_wiki(owner, "quiet")
    client = portal.test_client()
    login(client, owner)
    today = utcnow().date()
    # A wiki reports every day of the period, zeros included.
    daily = [{"day": (today - timedelta(days=offset)).isoformat(), "request": 0, "page_view": 0, "error": 0}
             for offset in range(89, -1, -1)]
    daily[-4].update(request=5, page_view=3)
    monkeypatch.setattr(runtime, "analytics", lambda spec, days: _summary(daily))
    page = client.get(f"/instances/{inst['id']}/analytics?days=90").get_data(as_text=True)
    assert page.count('<td class="date">') == 1
    assert f'<td class="date">{daily[-4]["day"]}</td>' in page
    assert "No traffic in this period." not in page
    daily[-4].update(request=0, page_view=0)
    page = client.get(f"/instances/{inst['id']}/analytics?days=90").get_data(as_text=True)
    assert '<td class="date">' not in page
    assert '<td colspan="4" class="muted">No traffic in this period.</td>' in page


# Banners, form patterns and copy buttons


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _patterns(page):
    return [html.unescape(value) for value in re.findall(r'\spattern="([^"]*)"', page)]


def test_the_wiki_name_pattern_escapes_its_hyphen(portal, make_account, login):
    client = portal.test_client()
    login(client, make_account())
    page = client.get("/instances/create").get_data(as_text=True)
    # Browsers read pattern with the "v" flag, where a bare "-" in a class is a syntax error and the
    # field then counts as always valid.
    assert _patterns(page) == [r"[a-z0-9\-]+"]


@needs_node
def test_portal_patterns_compile_as_browsers_read_them(portal, make_account, login):
    client = portal.test_client()
    login(client, make_account())
    patterns = _patterns(client.get("/instances/create").get_data(as_text=True))
    patterns += _patterns(portal.test_client().get("/signup").get_data(as_text=True))
    assert len(patterns) >= 1
    check = ("const bad = JSON.parse(process.argv[1]).filter((p) => {"
             " try { new RegExp('^(?:' + p + ')$', 'v'); return false; } catch (e) { return true; } });"
             " process.stdout.write(JSON.stringify(bad));")
    done = subprocess.run([NODE, "-e", check, json.dumps(patterns)], capture_output=True, text=True, timeout=60,
                          check=True)
    assert json.loads(done.stdout) == []


COPY_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const scenario = JSON.parse(process.argv[3]);

let clipboard = "", selected = null, focused = null;
const toasts = [], listeners = {};
const button = {
  getAttribute: (name) => (name === "data-copy" ? "S3cret-pass" : null),
  closest: (selector) => (selector === "[data-copy]" ? button : null),
  focus: () => { focused = button; },
};
focused = button;
const body = { appendChild: () => {}, classList: { contains: () => false, remove: () => {} } };
const document = {
  readyState: "loading",
  body,
  get activeElement() { return focused; },
  addEventListener: (type, fn) => { listeners[type] = fn; },
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => {
    const area = { value: "", style: {}, setAttribute: () => {}, remove: () => { if (selected === area) selected = null; } };
    area.focus = () => { focused = area; };
    area.select = () => { selected = area; };
    return area;
  },
  execCommand: (command) => {
    if (scenario.selection === "throws") throw new Error("not allowed");
    if (command !== "copy" || scenario.selection !== "copies" || !selected) return false;
    clipboard = selected.value;
    return true;
  },
};
const navigator = {};
if (scenario.clipboard) {
  navigator.clipboard = {
    writeText: (text) => {
      if (scenario.clipboard === "refuses") return Promise.reject(new Error("NotAllowedError"));
      clipboard = text;
      return Promise.resolve();
    },
  };
}
const window = { addEventListener: () => {}, location: { href: "http://portal.lan/" } };

(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"),
                     { window, document, navigator, setTimeout, clearTimeout, Headers: function () {} });
  window.BW.toast = (message, kind) => { toasts.push([message, kind]); };
  listeners.click({ target: button, preventDefault: () => {} });
  await new Promise((resolve) => setImmediate(resolve));
  process.stdout.write(JSON.stringify({ clipboard, toasts, focusBack: focused === button }));
})().catch((error) => { process.stdout.write(JSON.stringify({ crashed: String((error && error.stack) || error) })); });
"""


COPY_SCRIPTS = [HOSTING / "static" / "js" / "bananawiki.js", HOSTING.parent / "wiki" / "static" / "js" / "bananawiki.js"]


def _copy(tmp_path, script, **scenario):
    harness = tmp_path / "copy-harness.js"
    harness.write_text(COPY_HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), json.dumps(scenario)], capture_output=True, text=True,
                          timeout=60, check=True)
    result = json.loads(done.stdout)
    assert "crashed" not in result, result.get("crashed")
    return result


@needs_node
@pytest.mark.parametrize("script", COPY_SCRIPTS, ids=["portal", "wiki"])
def test_copy_buttons_copy_without_the_clipboard_api_and_always_say_what_happened(tmp_path, script):
    # A secure page uses the Clipboard API.
    assert _copy(tmp_path, script, clipboard="writes", selection="copies") == {
        "clipboard": "S3cret-pass", "toasts": [["copied", "success"]], "focusBack": True}
    # Plain HTTP has no navigator.clipboard: the text is copied from a selection, and focus returns to the button.
    assert _copy(tmp_path, script, clipboard=None, selection="copies") == {
        "clipboard": "S3cret-pass", "toasts": [["copied", "success"]], "focusBack": True}
    # A refused permission falls back the same way.
    assert _copy(tmp_path, script, clipboard="refuses", selection="copies")["toasts"] == [["copied", "success"]]
    # When nothing can copy, the person is told to copy by hand instead of being left believing it worked.
    for selection in ("fails", "throws"):
        result = _copy(tmp_path, script, clipboard=None, selection=selection)
        assert result["clipboard"] == "" and result["toasts"] == [["copy_failed", "error"]], selection
