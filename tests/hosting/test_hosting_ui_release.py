"""Regressions of the second 1.7.0 review of the portal interface: what blocked accounts are offered,
forms that keep their stored values, help languages, layout helpers and wording."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from bananawiki.core.timeutil import to_sql, utcnow
from bananawiki.hosting import accounts, auth, templating
from bananawiki.hosting.db import connection_scope

from .hosting_support import BOOTSTRAP, PASSWORD, build_portal, portal_environ

HOSTING = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting"


def _italian(client) -> None:
    client.post("/language", data={"language": "it"})


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


# ── Accounts held on one page by a gate ──────────────────────────────────────


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


# ── Forms that show what is stored ────────────────────────────────────────────


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
    # The 1.4 column was created with DEFAULT 0 ("delete at once"); the documented default is one day.
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


# ── Wikis ─────────────────────────────────────────────────────────────────────


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


def test_a_pending_outgoing_transfer_shows_on_the_dashboard(portal, make_account, make_wiki, login):
    giver = make_account()
    make_account("receiver")
    inst = make_wiki(giver, "handed-over")
    client = portal.test_client()
    login(client, giver)
    client.post(f"/instances/{inst['id']}/transfer", data={"username": "receiver"})
    assert "Transfer to receiver pending" in client.get("/dashboard").get_data(as_text=True)


def test_the_analytics_chart_has_one_bar_per_day(portal, make_account, make_wiki, login):
    owner = make_account()
    inst = make_wiki(owner, "counted")
    client = portal.test_client()
    login(client, owner)
    page = client.get(f"/instances/{inst['id']}/analytics?days=30").get_data(as_text=True)
    bars = re.search(r'<div class="bars[^"]*"[^>]*>(.*?)</div>', page, re.S).group(1)
    assert bars.count("<span ") == 30


def test_the_fake_runtime_reports_its_traffic_inside_the_period(portal, make_account, make_wiki, login):
    # Like a real wiki, the demo runtime reports every day of the window, ending today: its traffic shows in
    # the chart and the table instead of a fixed date outside every period.
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


def test_dns_values_break_after_dots_and_can_each_be_copied(tmp_path):
    portal = build_portal(tmp_path, environ=portal_environ(tmp_path, HOSTING_CUSTOM_DOMAIN_TARGET="edge.wiki.test"))
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]) as db:
        from bananawiki.hosting import instances

        owner = accounts.create("domain-owner", PASSWORD)
        inst = instances.create(owner, "with-domain")[0]
        db.execute("UPDATE instances SET custom_domain_allowed = 1 WHERE id = ?", (inst["id"],))
    client = portal.test_client()
    client.post("/login", data={"username": "domain-owner", "password": PASSWORD})
    client.post(f"/instances/{inst['id']}/domain", data={"action": "claim", "domain": "docs.example.org"})
    page = client.get(f"/instances/{inst['id']}/domain").get_data(as_text=True)
    # Each label is kept whole (also at a hyphen); lines break only after a dot.
    assert ('<span class="host-part">_bananawiki-challenge.</span><wbr><span class="host-part">docs.</span><wbr>'
            '<span class="host-part">example.</span><wbr><span class="host-part">org</span>') in page
    # The CNAME target is a value of its own, with its own Copy button.
    assert re.search(r'<dt>Points to</dt><dd class="kv-copy"><span class="mono"><span class="host-part">edge.</span>'
                     r'.*?</span> <button type="button" class="btn btn--small" data-copy="edge.wiki.test">', page)


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


# ── Help ──────────────────────────────────────────────────────────────────────


def test_help_in_the_language_asked_for_is_wholly_in_that_language(web):
    page = web.get("/help/features?lang=it", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert '<html lang="it"' in page
    assert '<a href="/help?lang=it">Guida</a>' in page
    assert 'href="/help/plugins?lang=it"' in page and 'href="/help/plugins"' not in page
    index = web.get("/help?lang=it", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert "<h1>Guida</h1>" in index and '<html lang="it"' in index
    # Without ?lang= the interface language decides, and links stay plain.
    english = web.get("/help/features", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert '<html lang="en"' in english and 'href="/help/plugins"' in english


def test_help_marks_the_plugins_a_new_wiki_starts_with():
    import importlib
    import pkgutil

    import bananawiki.wiki.features as features
    from bananawiki.wiki.registry import Feature

    on = set()
    for module in pkgutil.iter_modules(features.__path__):
        feature = getattr(importlib.import_module(f"{features.__name__}.{module.name}"), "FEATURE", None)
        if isinstance(feature, Feature) and feature.toggle == "plugin" and feature.default_enabled:
            on.add(feature.id)
    names = {"announcements": "Announcements", "assessments": "Assessments", "attachments": "Attachments",
             "audit": "Audit log", "badges": "Badges", "canvas": "Canvas", "chat": "Chats and groups",
             "difficulty_tags": "Difficulty tags", "drafts": "Drafts", "kanban": "Kanban boards", "mascot": "Mascot",
             "page_history": "Page history", "tts": "Read aloud", "user_data_export": "Personal data export",
             "user_profiles": "User profiles"}
    assert on == set(names)
    help_en = json.loads((HOSTING / "content" / "help.en.json").read_text(encoding="utf-8"))
    plugins = next(a for a in help_en if a["slug"] == "plugins")
    listed = " ".join(section["html"] for section in plugins["sections"])
    marked = set(re.findall(r"<li><strong>([^<]+)</strong> <em>\(on\)</em>", listed))
    assert marked == set(names.values())


@pytest.mark.parametrize("language", ["de", "en", "it"])
def test_help_names_every_plugin_a_new_wiki_starts_with(language):
    """"Switched on from the start" names each plugin a new wiki starts with (the Mascot too), as the wiki's
    Plugins page calls it in that language."""
    import importlib
    import pkgutil

    import bananawiki.wiki.features as features
    from bananawiki.wiki.registry import Feature

    package = HOSTING.parent
    on = []
    for module in pkgutil.iter_modules(features.__path__):
        feature = getattr(importlib.import_module(f"{features.__name__}.{module.name}"), "FEATURE", None)
        if isinstance(feature, Feature) and feature.toggle == "plugin" and feature.default_enabled:
            on.append(feature)
    names = {}
    for path in package.rglob(f"translations/{language}.json"):
        names.update(json.loads(path.read_text(encoding="utf-8")))
    articles = json.loads((HOSTING / "content" / f"help.{language}.json").read_text(encoding="utf-8"))
    started = next(a for a in articles if a["slug"] == "features")["sections"][6]["html"]
    first_sentence = started.split(". ", 1)[0]
    missing = [names[feature.name] for feature in on if names[feature.name].casefold() not in first_sentence.casefold()]
    assert missing == []


def test_help_uses_straight_apostrophes_like_the_interface():
    for language in ("de", "en", "it"):
        assert "’" not in (HOSTING / "content" / f"help.{language}.json").read_text(encoding="utf-8")


# ── Wording and helpers ───────────────────────────────────────────────────────


def test_past_moments_agree_in_number_and_say_just_now(portal):
    with portal.test_request_context("/", headers={"Accept-Language": "it"}):
        assert templating.time_ago(to_sql(utcnow() - timedelta(days=1, hours=2))) == "1 giorno fa"
        assert templating.time_ago(to_sql(utcnow() - timedelta(seconds=20))) == "adesso"
    with portal.test_request_context("/", headers={"Accept-Language": "en"}):
        assert templating.time_ago(to_sql(utcnow() - timedelta(days=1, hours=2))) == "1 day ago"
        assert templating.time_ago(to_sql(utcnow() - timedelta(days=400))) == "1 year ago"
        assert templating.duration(14 * 86400) == "14d"
        assert templating.format_datetime_long("2026-10-14 16:39:00") == "14 October 2026, 16:39 UTC"
    with portal.test_request_context("/", headers={"Accept-Language": "it"}):
        assert templating.format_datetime_long("2026-10-14 16:39:00") == "14 ottobre 2026, ore 16:39 UTC"


def test_german_dates_and_durations_read_as_german(portal):
    """German counts in the dative after "vor" and "in" (every use of the long duration follows "in"),
    writes the day with a full stop and abbreviates units the German way."""
    with portal.test_request_context("/", headers={"Accept-Language": "de"}):
        assert templating.time_ago(to_sql(utcnow() - timedelta(days=1, hours=2))) == "vor 1 Tag"
        assert templating.time_ago(to_sql(utcnow() - timedelta(days=3, hours=2))) == "vor 3 Tagen"
        assert templating.time_ago(to_sql(utcnow() - timedelta(seconds=20))) == "gerade eben"
        assert templating.format_datetime_long("2026-10-14 16:39:00") == "14. Oktober 2026, 16:39 UTC"
        assert templating.duration_long(3 * 86400 + 4 * 3600) == "3 Tagen und 4 Stunden"
        assert templating.duration_long(86400 + 3600) == "1 Tag und 1 Stunde"
        assert templating.duration(14 * 86400) == "14 Tg."
        assert templating.megabytes(163.4) == "163,4"


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


def test_bulk_actions_that_destroy_look_destructive(portal, make_account, login):
    admin = make_account(admin=True)
    client = portal.test_client()
    login(client, admin)
    page = client.get("/admin").get_data(as_text=True)
    for action in ("/admin/instances/bulk-delete", "/admin/instances/bulk-delete-terminated"):
        button = re.search(r'<button class="([^"]*)" formaction="' + re.escape(action) + '"', page)
        assert button and "btn--danger" in button.group(1), action
    stop = re.search(r'<button class="([^"]*)" formaction="/admin/instances/bulk-stop"', page)
    assert stop and "btn--danger" not in stop.group(1)
    assert "Select accounts in the list, then choose an action." in page


def test_portal_styles_keep_the_second_round_fixes():
    css = (HOSTING / "static" / "css" / "hosting.css").read_text(encoding="utf-8")
    # Recovery codes: a column is never narrower than a code.
    assert "minmax(11ch" not in css and "minmax(14.5ch, 1fr)" in css
    # The login page's separator dot is drawn by CSS and hidden when the second link wraps.
    assert '.recovery-links a + a::before { content: "·"; content: "·" / "";' in css
    # A small number field next to small buttons is as tall as they are.
    assert re.search(r"\.hosting-num--sm \{ height: var\(--control-height-sm\) !important;", css)
    # Stacked tables on phones line up with the headings: no panel behind them.
    assert ".table-wrap:has(> .table--stack) { background: none; border-inline: 0; }" in css
    # Disabled fields look unavailable.
    assert "fieldset:disabled :is(input, select, textarea)" in css


def test_the_desktop_launcher_treats_wiki_as_feminine_like_the_web_apps():
    masculine = re.compile(r"\b(il|un|questo|del|nel|dal|al|sul) wiki\b|\bwiki (è |si è )?(fermo|attivo|fermato|condiviso)\b", re.I)
    catalogue = json.loads((HOSTING.parent / "desktop" / "translations" / "it.json").read_text(encoding="utf-8"))
    assert not [key for key, value in catalogue.items() if masculine.search(value)]
    assert catalogue["status.stopped"] == "La wiki è ferma."
