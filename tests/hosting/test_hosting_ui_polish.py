"""Regressions of the 1.7.0 interface review of the portal: wording, languages, markup and styles."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.hosting import i18n, templating
from bananawiki.hosting.app import _MaintenanceGate
from bananawiki.hosting.db import connection_scope

from .hosting_support import BOOTSTRAP, PASSWORD, build_portal, portal_environ

HOSTING = Path(__file__).resolve().parents[2] / "bananawiki" / "hosting"
MB = 1024 * 1024
STORAGE_REASON = "Automatic storage protection: 130.0 MB used, 100 MB allowed."


def _italian(client) -> None:
    client.post("/language", data={"language": "it"})


def _in(portal, lang: str):
    """A request context in *lang* for calling the template helpers directly."""
    context = portal.test_request_context("/", headers={"Accept-Language": lang})
    return context


# ── Template helpers ──────────────────────────────────────────────────────────


def test_durations_use_the_units_of_the_interface_language(portal):
    with _in(portal, "it"):
        assert templating.duration(3 * 86400 + 4 * 3600) == "3 g 4 h"
        assert templating.duration(400 * 86400) == "1 a 35 g"
        assert templating.duration(2 * 3600 + 5 * 60) == "2 h 5 min"
        assert templating.duration_long(2 * 86400 + 23 * 3600) == "2 giorni e 23 ore"
        assert templating.duration_long(86400) == "1 giorno"
    with _in(portal, "en"):
        assert templating.duration(3 * 86400 + 4 * 3600) == "3d 4h"
        assert templating.duration_long(3600 + 60) == "1 hour and 1 minute"


def test_sizes_drop_the_pointless_decimal_and_use_the_italian_comma(portal):
    with _in(portal, "it"):
        assert templating.megabytes(42.0) == "42"
        assert templating.megabytes(163.4) == "163,4"
    with _in(portal, "de"):
        assert templating.megabytes(163.4) == "163,4"
    with _in(portal, "en"):
        assert templating.megabytes(163.4) == "163.4"


def test_reasons_the_platform_writes_are_shown_in_the_visitors_language(portal):
    with _in(portal, "it"):
        # A number and its unit never part at a line break (no-break space).
        assert templating.server_text(STORAGE_REASON) == \
            "Protezione automatica dello spazio: 130\u00a0MB occupati su 100\u00a0MB consentiti."
        detail = ("its files take more than the 110 MiB its storage limit allows in a backup (a sparse file at its "
                  "full size); stopped at uploads/raw/IMG_2041.CR3")
        assert templating.server_text(detail).startswith("Interrotto a uploads/raw/IMG_2041.CR3")
        assert "Automatic" not in templating.server_text("Automatic storage protection: 5.0 MB used, 1 MB allowed.")
        # An administrator's own words are shown as written.
        assert templating.server_text("Repeated spam") == "Repeated spam"
        assert templating.event_label("instance.suspend") == "Sospesa"
        assert templating.event_label("instance.terminated.something_new") == "Chiusa"
        assert templating.event_label("unknown.code") == "unknown.code"


def test_every_event_the_portal_records_has_a_label_in_each_language(portal):
    """The moderation log showed the raw code of events without a label: plugins.quarantined (the button and
    updates), storage.over_plan (a wiki adopted above its plan), oauth.linked, oauth.rotated and the platform
    backup's exports and restores."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in HOSTING.rglob("*.py"))
    codes = set(re.findall(r'events\.record\([^,]+, [^,]+, "([a-z_.]+)"[,)]', source))
    # Codes built from a literal and a choice: "account.admin." + ("granted" if promote else "revoked"), ...
    for prefix, first, second in re.findall(
            r'events\.record\([^,]+, [^,]+, "([a-z_.]+)" \+ \("([a-z_]+)" if [^"]+ else "([a-z_]+)"\)', source):
        codes |= {prefix + first, prefix + second}
    assert {"plugins.quarantined", "storage.over_plan", "account.admin.granted"} <= codes and len(codes) > 40
    for language in i18n.LANGUAGES:
        with _in(portal, language):
            assert [code for code in sorted(codes) if templating.event_label(code) == code] == [], language


def test_english_calls_two_step_verification_by_one_name():
    # One string still said "two-step sign-in", next to "two-step verification" everywhere else (and in the
    # Italian and German strings).
    catalogue = json.loads((HOSTING / "translations" / "en.json").read_text(encoding="utf-8"))
    assert [key for key, value in catalogue.items() if re.search(r"two.step sign.in", str(value))] == []


# ── Pages ─────────────────────────────────────────────────────────────────────


def test_the_maintenance_gate_answers_browsers_in_their_language():
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)
    seen: dict = {}

    def start(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(gate({"PATH_INFO": "/dashboard", "HTTP_ACCEPT": "text/html,*/*",
                          "HTTP_ACCEPT_LANGUAGE": "it-IT,it;q=0.9"}, start)).decode()
    assert seen["status"].startswith("503") and seen["headers"]["Content-Type"].startswith("text/html")
    assert '<html lang="it">' in body and "Manutenzione in corso" in body and "Maintenance" not in body
    assert seen["headers"]["Retry-After"] and seen["headers"]["Cache-Control"] == "no-store"
    english = b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html"}, start)).decode()
    assert '<html lang="en">' in english and "Maintenance in progress" in english
    plain = b"".join(gate({"PATH_INFO": "/api/v1/instances", "HTTP_ACCEPT": "application/json"}, start))
    assert seen["headers"]["Content-Type"].startswith("text/plain") and b"Maintenance" in plain


def test_the_maintenance_gate_has_a_page_in_every_portal_language():
    """German too; a bare ``*`` (no preference) gets English, not the first language by code."""
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)

    def page(**environ) -> str:
        return b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", **environ},
                             lambda status, headers: None)).decode()

    for language in i18n.LANGUAGES:
        assert f'<html lang="{language}">' in page(HTTP_ACCEPT_LANGUAGE=f"{language};q=1")
        assert f'<html lang="{language}">' in page(HTTP_COOKIE=f"bw_lang={language}")
    assert '<html lang="en">' in page(HTTP_ACCEPT_LANGUAGE="*")
    assert '<html lang="en">' in page(HTTP_ACCEPT_LANGUAGE="de, en")  # a tie that includes English


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


def test_the_two_step_code_accepts_recovery_codes_on_phones(web, make_account, query):
    account = make_account("twostep")
    query("UPDATE accounts SET totp_enabled = 1 WHERE id = ?", (account["id"],))
    web.post("/login", data={"username": "twostep", "password": PASSWORD})
    page = web.get("/login/mfa").get_data(as_text=True)
    code = re.search(r'<input id="f-code"[^>]*>', page).group(0)
    assert 'inputmode="numeric"' not in code and 'autocapitalize="characters"' in code
    assert 'href="/login"' in page


def test_the_not_found_page_is_titled_in_words(web):
    page = web.get("/no-such-page").get_data(as_text=True)
    assert "<h1>Page not found</h1>" in page and 'class="error-code" aria-hidden="true">404' in page
    assert "Reference:" not in page


def test_a_deleted_wiki_offers_its_data_not_a_password_reset(portal, make_account, make_wiki, login):
    owner = make_account(admin=True)
    inst = make_wiki(owner, "gone-wiki")
    client = portal.test_client()
    login(client, owner)
    client.post(f"/instances/{inst['id']}/terminate")
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    assert "Download your data" in page and "Locked out" not in page
    assert "Largest upload" not in page


def test_a_storage_suspension_is_explained_in_italian_everywhere(portal, runtime, make_account, make_wiki, login,
                                                                 query):
    owner = make_account("photos")
    inst = make_wiki(owner, "photo-lab")
    query("UPDATE instances SET storage_limit_mb = 100 WHERE id = ?", (inst["id"],))
    runtime.tenants[inst["subdomain"]].usage = 130 * MB
    with portal.app_context(), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import instances

        instances.enforce_storage_quotas()
    client = portal.test_client()
    login(client, owner)
    _italian(client)
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    # The badge is in sentence case like every other status badge.
    assert "Automatic storage protection" not in page and '<span class="badge badge--danger">Oltre il limite</span>' in page
    assert "progress--danger" in page
    status = portal.test_client().get("/", headers={"Host": "photo-lab-hosting.wiki.test",
                                                    "Accept-Language": "it"}).get_data(as_text=True)
    assert "Protezione automatica dello spazio" in status and "Automatic storage protection" not in status
    assert "Questa wiki è sospesa" in status


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


def test_token_expiry_and_permissions_are_in_words(portal, make_account, login, query):
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    owner = make_account()
    client = portal.test_client()
    login(client, owner)
    _italian(client)
    page = client.get("/account").get_data(as_text=True)
    assert '<option value="30">30 giorni</option>' in page and '<option value="365">1 anno</option>' in page
    assert "Lettura dei dati del tuo account <code>account:read</code>" in page


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


def test_italian_summary_counts_agree_with_their_number(portal, make_account, make_wiki, login):
    owner = make_account()
    make_wiki(owner)
    client = portal.test_client()
    login(client, owner)
    _italian(client)
    page = client.get("/dashboard").get_data(as_text=True)
    assert "<dt>arrestate</dt><dd>0</dd>" in page and "<dt>in esecuzione</dt><dd>1</dd>" in page


# ── Catalogues and styles ─────────────────────────────────────────────────────


MASCULINE_WIKI = re.compile(
    r"\b(il|del|al|dal|nel|sul|un|questo|quel|nessun|tuo|suo|nuovo|primo|i|dei|ai|dai|nei|sui|tuoi|suoi|questi|quei"
    r"|nuovi|altri) wiki\b|\bwiki (?:è |sono )?(?:pront[oi]|sospes[oi]|chius[oi]|scadut[oi]|attiv[oi]|arrestat[oi]"
    r"|selezionati|elencati|condivis[oi]|autorizzat[oi])\b", re.I)


def test_italian_treats_wiki_as_feminine_like_the_wiki_and_the_site():
    catalogue = json.loads((HOSTING / "translations" / "it.json").read_text(encoding="utf-8"))
    found = [key for key, value in catalogue.items() if isinstance(value, str) and MASCULINE_WIKI.search(value)]
    assert not found
    help_text = (HOSTING / "content" / "help.it.json").read_text(encoding="utf-8")
    assert not MASCULINE_WIKI.search(help_text)
    # The wiki's own confirmation label is quoted as the wiki shows it (site_admin.migration.confirm_label).
    assert "Ho capito che questo sostituisce tutti i dati di questa wiki" in help_text
    # Labels and log lines about a wiki agree with it; an account keeps its own masculine badge.
    assert catalogue["hosting.status.suspended"] == "Sospesa" and catalogue["hosting.event.instance.started"] == "Avviata"
    assert catalogue["hosting.admin.account_suspended_badge"] == "Sospeso"
    assert catalogue["hosting.bulk.stop"] == "Arresta selezionate"


GERMAN_FEMININE_WIKI = re.compile(
    r"\b(die|der|eine|einer|keine|keiner|deine|deiner|diese|dieser|jede|jeder|ihre|ihrer|seine|seiner) Wiki(?![-\w])")


def test_german_treats_wiki_as_neuter_and_addresses_the_reader_with_du():
    """German says "das Wiki" (as the wiki's own German catalogues do) and "du", like the rest of Dhareck's
    translation: no feminine "die Wiki" carried over from Italian, no formal "Sie" in new strings."""
    catalogue = json.loads((HOSTING / "translations" / "de.json").read_text(encoding="utf-8"))
    help_text = (HOSTING / "content" / "help.de.json").read_text(encoding="utf-8")
    texts = {key: value for key, value in catalogue.items() if isinstance(value, str)}
    texts["help"] = help_text
    assert [key for key, value in texts.items() if GERMAN_FEMININE_WIKI.search(value)] == []
    # Nor at the start of a sentence, where "Ihr"/"Ihre" meaning "its"/"their" reads as the formal "your".
    formal = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b")
    assert [key for key, value in texts.items() if formal.search(value)] == []
    assert catalogue["hosting.status.suspended"] == "Gesperrt" and catalogue["hosting.instance.terminate"] == "Stilllegen"


def test_the_maintenance_page_is_written_in_each_language():
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)
    for language in i18n.LANGUAGES:
        catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
        body = b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", "HTTP_ACCEPT_LANGUAGE": language},
                             lambda status, headers: None)).decode()
        assert f"<h1>{catalogue['hosting.maintenance.title']}</h1>" in body, language
        assert language == "en" or "Maintenance in progress" not in body, language


def test_the_portal_uses_the_wikis_italian_names_for_shared_features():
    catalogue = json.loads((HOSTING / "translations" / "it.json").read_text(encoding="utf-8"))
    help_text = (HOSTING / "content" / "help.it.json").read_text(encoding="utf-8")
    wiki_it = json.loads((HOSTING.parent / "wiki" / "features" / "page_builder" / "translations" / "it.json")
                         .read_text(encoding="utf-8"))
    assert catalogue["hosting.features.page_builder"] == wiki_it["feature.page_builder.name"] == "Editor visuale delle pagine"
    text = " ".join(v for v in catalogue.values() if isinstance(v, str)) + help_text
    for stale in ("ostruttore di pagine", "visita guidata", "sintesi vocale"):
        assert stale not in text.lower(), stale


def test_help_names_the_buttons_the_portal_shows():
    english = (HOSTING / "content" / "help.en.json").read_text(encoding="utf-8")
    italian = (HOSTING / "content" / "help.it.json").read_text(encoding="utf-8")
    for stale in ("New Instance", "<strong>Pause</strong>", "Admin Operations", "Download Site Export"):
        assert stale not in english
    for stale in ("Nuova istanza", "<strong>Pausa</strong>", "Operazioni admin", "Chats and Groups"):
        assert stale not in italian


def test_portal_styles_keep_the_mobile_fixes():
    css = (HOSTING / "static" / "css" / "hosting.css").read_text(encoding="utf-8")
    # The portal's phone top bar holds only the brand and the menu toggle: the brand is not cut short.
    assert ".hosting-topbar .topbar__brand span { max-width: none; }" in css
    # Help index entries stay stacked on touch screens.
    assert re.search(r"@media \(pointer: coarse\) \{[^@]*\.link-list a \{ flex-direction: column;", css)
    # Recovery codes have no list bullets over the box border.
    assert "ul.secret-box { list-style: none;" in css
    # A small form after another one in a disclosure is a block, so its separator is drawn.
    assert "details.card > form + form.inline, details.card > form.inline + form { display: block; }" in css
    # The dark danger colour is the shared design system's (one value for the wiki and the portal).
    assert not re.search(r"--danger\s*:", css.split('[data-theme="light"] {', 1)[0])
    assert not re.search(r':root:not\(\[data-theme="light"\]\)\s*\{[^}]*--danger', css)


def test_a_long_feature_name_stays_in_its_column():
    """German "Seitenbaukasten" is one word wider than the name column of a wiki's optional features on a
    phone: it is hyphenated (or broken) there instead of running under the "Request access" button. Only German
    pages hyphenate, as in the wiki: English and Italian names keep wrapping between whole words."""
    css = (HOSTING / "static" / "css" / "hosting.css").read_text(encoding="utf-8")
    assert ".setting-row h3 { overflow-wrap: anywhere; }" in css
    assert ":lang(de) .setting-row h3 { hyphens: auto; }" in css
    assert ".setting-row h3 { overflow-wrap: anywhere; hyphens: auto; }" not in css


def _contrast(first: str, second: tuple[int, ...]) -> float:
    def luminance(rgb):
        def channel(value):
            value /= 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        red, green, blue = map(channel, rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    rgb = tuple(int(first.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    high, low = sorted((luminance(rgb), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_shared_dark_danger_colour_is_readable_on_its_tint():
    core = (HOSTING.parent / "core" / "static" / "css" / "bananawiki.css").read_text(encoding="utf-8")
    dark = core.split('[data-theme="light"] {', 1)[0]
    danger = re.search(r"--danger: (#[0-9a-f]{6});", dark).group(1)
    assert f"--danger-bg: color-mix(in srgb, {danger} 12%, var(--surface));" in dark
    secondary, background = (tuple(int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for c in ("#1e1e2c", "#16161f"))
    surface = tuple(round(s * .75 + b * .25) for s, b in zip(secondary, background, strict=True))
    tint = tuple(round(d * .12 + s * .88) for d, s in zip(
        (int(danger[i:i + 2], 16) for i in (1, 3, 5)), surface, strict=True))
    assert _contrast(danger, tint) >= 4.5 and _contrast(danger, surface) >= 4.5


@pytest.mark.parametrize("language", sorted(i18n.LANGUAGES))
def test_new_strings_exist_in_every_language(language):
    catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
    for key in ("time.short.days", "time.long.pair", "hosting.server_text.storage", "hosting.maintenance.title",
                "hosting.signup.accept_terms_sentence", "hosting.wiki_role.owner", "hosting.event.instance.suspend",
                "hosting.instance.download_title", "hosting.status.expired", "hosting.breadcrumbs"):
        assert catalogue.get(key), key
