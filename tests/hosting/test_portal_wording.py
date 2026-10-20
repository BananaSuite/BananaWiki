"""Portal wording: languages, catalogues, help articles and the helpers that format dates, sizes and reasons."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from bananawiki.core.timeutil import to_sql, utcnow
from bananawiki.hosting import i18n, templating
from bananawiki.hosting.app import _MaintenanceGate
from bananawiki.hosting.db import connection_scope

PACKAGE = Path(__file__).resolve().parents[2] / "bananawiki"
HOSTING = PACKAGE / "hosting"
MB = 1024 * 1024
STORAGE_REASON = "Automatic storage protection: 130.0 MB used, 100 MB allowed."


def _italian(client) -> None:
    client.post("/language", data={"language": "it"})


def _in(portal, lang: str):
    """A request context in *lang* for calling the template helpers directly."""
    context = portal.test_request_context("/", headers={"Accept-Language": lang})
    return context


# Dates, durations and sizes


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


# Reasons and events


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
    """The moderation log shows a label, never the raw code, for every event the portal records."""
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


# Catalogues


@pytest.mark.parametrize("language", sorted(i18n.LANGUAGES))
def test_portal_strings_exist_in_every_language(language):
    catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
    for key in ("time.short.days", "time.long.pair", "hosting.server_text.storage", "hosting.maintenance.title",
                "hosting.signup.accept_terms_sentence", "hosting.wiki_role.owner", "hosting.event.instance.suspend",
                "hosting.instance.download_title", "hosting.status.expired", "hosting.breadcrumbs",
                "hosting.analytics.no_traffic"):
        assert catalogue.get(key), key


def test_english_calls_two_step_verification_by_one_name():
    # "Two-step verification" everywhere, as in the Italian and German strings; never "two-step sign-in".
    catalogue = json.loads((HOSTING / "translations" / "en.json").read_text(encoding="utf-8"))
    assert [key for key, value in catalogue.items() if re.search(r"two.step sign.in", str(value))] == []


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


def test_the_desktop_launcher_treats_wiki_as_feminine_like_the_web_apps():
    masculine = re.compile(r"\b(il|un|questo|del|nel|dal|al|sul) wiki\b|\bwiki (è |si è )?(fermo|attivo|fermato|condiviso)\b", re.I)
    catalogue = json.loads((HOSTING.parent / "desktop" / "translations" / "it.json").read_text(encoding="utf-8"))
    assert not [key for key, value in catalogue.items() if masculine.search(value)]
    assert catalogue["status.stopped"] == "La wiki è ferma."


def test_italian_summary_counts_agree_with_their_number(portal, make_account, make_wiki, login):
    owner = make_account()
    make_wiki(owner)
    client = portal.test_client()
    login(client, owner)
    _italian(client)
    page = client.get("/dashboard").get_data(as_text=True)
    assert "<dt>arrestate</dt><dd>0</dd>" in page and "<dt>in esecuzione</dt><dd>1</dd>" in page


def test_token_expiry_and_permissions_are_in_words(portal, make_account, login, query):
    query("UPDATE hosting_settings SET api_enabled = 1 WHERE id = 1")
    owner = make_account()
    client = portal.test_client()
    login(client, owner)
    _italian(client)
    page = client.get("/account").get_data(as_text=True)
    assert '<option value="30">30 giorni</option>' in page and '<option value="365">1 anno</option>' in page
    assert "Lettura dei dati del tuo account <code>account:read</code>" in page


GERMAN_FEMININE_WIKI = re.compile(
    r"\b(die|der|eine|einer|keine|keiner|deine|deiner|diese|dieser|jede|jeder|ihre|ihrer|seine|seiner) Wiki(?![-\w])")


def test_german_treats_wiki_as_neuter_and_addresses_the_reader_with_du():
    """German says "das Wiki" (as the wiki's own German catalogues do) and "du": never "die Wiki" or "Sie"."""
    catalogue = json.loads((HOSTING / "translations" / "de.json").read_text(encoding="utf-8"))
    help_text = (HOSTING / "content" / "help.de.json").read_text(encoding="utf-8")
    texts = {key: value for key, value in catalogue.items() if isinstance(value, str)}
    texts["help"] = help_text
    assert [key for key, value in texts.items() if GERMAN_FEMININE_WIKI.search(value)] == []
    # Nor at the start of a sentence, where "Ihr"/"Ihre" meaning "its"/"their" reads as the formal "your".
    formal = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b")
    assert [key for key, value in texts.items() if formal.search(value)] == []
    assert catalogue["hosting.status.suspended"] == "Gesperrt" and catalogue["hosting.instance.terminate"] == "Stilllegen"


def test_the_portal_uses_the_wikis_italian_names_for_shared_features():
    catalogue = json.loads((HOSTING / "translations" / "it.json").read_text(encoding="utf-8"))
    help_text = (HOSTING / "content" / "help.it.json").read_text(encoding="utf-8")
    wiki_it = json.loads((HOSTING.parent / "wiki" / "features" / "page_builder" / "translations" / "it.json")
                         .read_text(encoding="utf-8"))
    assert catalogue["hosting.features.page_builder"] == wiki_it["feature.page_builder.name"] == "Editor visuale delle pagine"
    text = " ".join(v for v in catalogue.values() if isinstance(v, str)) + help_text
    for stale in ("ostruttore di pagine", "visita guidata", "sintesi vocale"):
        assert stale not in text.lower(), stale


@pytest.mark.parametrize("package", ["hosting", "wiki"])
def test_the_copy_failure_message_is_translated(package):
    messages = {language: json.loads((HOSTING.parent / package / "translations" / f"{language}.json")
                                     .read_text(encoding="utf-8"))["js.copy_failed"] for language in ("en", "it", "de")}
    assert len(set(messages.values())) == 3


# Maintenance page


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


def test_the_maintenance_page_is_written_in_each_language():
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)
    for language in i18n.LANGUAGES:
        catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
        body = b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", "HTTP_ACCEPT_LANGUAGE": language},
                             lambda status, headers: None)).decode()
        assert f"<h1>{catalogue['hosting.maintenance.title']}</h1>" in body, language
        assert language == "en" or "Maintenance in progress" not in body, language


# Help


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


def test_help_names_the_buttons_the_portal_shows():
    english = (HOSTING / "content" / "help.en.json").read_text(encoding="utf-8")
    italian = (HOSTING / "content" / "help.it.json").read_text(encoding="utf-8")
    for stale in ("New Instance", "<strong>Pause</strong>", "Admin Operations", "Download Site Export"):
        assert stale not in english
    for stale in ("Nuova istanza", "<strong>Pausa</strong>", "Operazioni admin", "Chats and Groups"):
        assert stale not in italian


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


def _feature_names(language: str) -> set[str]:
    names: set[str] = set()
    for path in PACKAGE.rglob(f"translations/{language}.json"):
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        names |= {value for key, value in catalogue.items() if re.fullmatch(r"feature\.[a-z_]+\.name", key)}
    return names


def _article(language: str, slug: str) -> str:
    articles = json.loads((HOSTING / "content" / f"help.{language}.json").read_text(encoding="utf-8"))
    return " ".join(section["html"] for section in next(a for a in articles if a["slug"] == slug)["sections"])


@pytest.mark.parametrize("language", sorted(i18n.LANGUAGES))
def test_the_plugin_list_uses_the_names_of_the_wiki_plugins_page(language):
    listed = re.findall(r"<li><strong>([^<]+)</strong>", _article(language, "plugins"))
    assert listed and not set(listed) - _feature_names(language)
    assert listed == sorted(listed, key=str.casefold)
    governance = next(name for name in _feature_names(language)
                      if name in ("Page governance", "Protezione e prenotazioni delle pagine",
                                  "Seitenschutz und Reservierungen"))
    assert f"<strong>{governance}</strong>" in _article(language, "features")


@pytest.mark.parametrize("language", sorted(i18n.LANGUAGES))
def test_account_help_names_the_wiki_page_sections(language):
    catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
    article = _article(language, "account-management")
    for key in ("hosting.instance.reset_or_delete", "hosting.instance.terminate"):
        assert f"<strong>{catalogue[key]}</strong>" in article, key
    assert "Reset or delete wiki" not in article and "Reimposta o elimina wiki" not in article
    assert "Wiki zurücksetzen oder löschen" not in article
