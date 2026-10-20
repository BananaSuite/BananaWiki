"""Interface wording: catalogues in every language, counted strings, terminology and formatting helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki import i18n, templating

ROOT = Path(__file__).resolve().parents[2] / "bananawiki"
WIKI = ROOT / "wiki"


def catalogue(lang: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(WIKI.glob(f"**/translations/{lang}.json")):
        if "examples" in path.parts:
            continue
        merged.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"})
    return merged


def feature_catalogue(feature: str, lang: str) -> dict[str, str]:
    path = WIKI / "features" / feature / "translations" / f"{lang}.json"
    return {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"}


def feature_file(*parts: str) -> str:
    return (WIKI / "features" / Path(*parts)).read_text(encoding="utf-8")


EN, IT, DE = catalogue("en"), catalogue("it"), catalogue("de")


# Catalogues


@pytest.mark.parametrize("key", [
    "common.breadcrumbs", "audit.detail.base_role", "audit.value.all", "attention.source.chat.reports",
    "chat.admin.ip", "chat.blocks.preview_hidden", "page_history.first_version", "custom_pages.view.copy",
    "contributions.usage_unlimited", "contributions.others_waiting_one", "contributions.quota.request_number",
    "js.kanban.due_overdue_on", "js.kanban.due_soon_on", "js.kanban.column_name", "js.kanban.list.moved",
    "kanban.mine.due_overdue", "site_admin.log.empty_errors", "site_admin.server.intro", "page_builder.settings.intro",
    "plugin_manager.order.hidden", "auth.two_step.settings_help_on", "auth.two_step.settings_help_off",
    "canvas.history.preview_hint", "users.members.publish_own", "profiles.edit_about", "admin.users.new_role",
])
def test_interface_strings_exist_in_every_language(key):
    assert EN.get(key) and IT.get(key) and DE.get(key) and IT[key] != key and DE[key] != key


def test_numbers_and_units_do_not_break_apart():
    # A no-break space keeps a number on the same line as its unit.
    assert "{limit_mb}\u00a0MB" in EN["site_admin.migration.file_hint"]
    assert "{limit_mb}\u00a0MB" in IT["site_admin.migration.file_hint"]


# Counted strings


def test_english_strings_have_no_plural_hacks():
    # Singular forms live in "<key>_one"; "1 account(s)" reads like a template.
    assert not [key for key, text in EN.items() if re.search(r"[a-z]\(s\)", text.replace("http(s)", ""))]


@pytest.mark.parametrize("key", [
    "badges.banner", "kanban.mine.count", "contributions.notice.waiting", "contributions.usage",
    "assessments.points_awarded", "assessments.question_count", "chat.groups.member_count", "chat.unread",
    "canvas.history.nodes", "canvas.history.edges", "time.days_ago", "time.months_ago", "time.years_ago",
    "bulk_manage.page_count", "admin.users.count", "js.kanban.tickets_count", "users.profile.contributions_in",
    "tts.admin.retried", "tts.admin.retries", "tts.admin.backfill", "tts.admin.cleared", "bulk_manage.done.boards",
    "bulk_manage.done.canvases", "bulk_manage.done.categories", "bulk_manage.done.categories_skipped",
    "bulk_manage.done.pages", "badges.flash.revoked_all", "api_service.flash.all_revoked",
    "api_service.flash.token_revoked_with_issued", "api_service.tokens.revoke_confirm_issued",
    "api_service.webhooks.failures", "page_governance.admin.cooldowns_cleared",
    "page_history.flash.bulk_transferred", "page_history.flash.cleared", "site_admin.languages.ignored_keys",
    "site_admin.migration.skipped", "site_admin.languages.error.placeholders", "js.plugin_manager.filter.matches",
    "js.page_builder.embed_picker.count", "admin.suspend.hours", "api_service.flash.audit_cleared",
    "page_export.import.more_problems",
])
def test_counted_strings_have_a_singular_in_every_language(key):
    assert key + "_one" in EN and key + "_one" in IT and key + "_one" in DE
    assert "{count}" not in EN[key + "_one"]


def test_scripts_count_with_the_singular_form():
    features = WIKI / "features"
    assert 'BW.tn("plugin_manager.filter.matches", count)' in \
        (features / "plugin_manager/static/plugins.js").read_text(encoding="utf-8")
    assert 'BW.tn("page_builder.embed_picker.count", items.length)' in \
        (features / "page_builder/static/page_builder.js").read_text(encoding="utf-8")


def test_counted_admin_labels_use_the_singular(app):
    with app.test_request_context("/?lang=de"):
        assert i18n.tn("api_service.webhooks.failures", 1) == "1 fehlgeschlagener Versuch"
        assert i18n.tn("tts.admin.retried", 1) == "1 Auftrag erneut in die Warteschlange gestellt."
    with app.test_request_context("/?lang=it"):
        assert i18n.tn("bulk_manage.done.boards", 1) == "1 bacheca eliminata."
        assert i18n.tn("bulk_manage.done.boards", 2) == "2 bacheche eliminate."
        assert i18n.tn("api_service.flash.audit_cleared", 1, days=30) == \
            "Rimossa 1 voce del registro delle chiamate più vecchia di 30 giorni."
        assert i18n.t("pages.upload.quota_count", count=1) == \
            "Hai raggiunto il limite giornaliero di caricamenti (1). Riprova domani."


def test_tn_picks_the_singular_only_when_the_language_has_one(app):
    with app.test_request_context("/?lang=it"):
        assert i18n.tn("badges.banner", 1) == "Hai ottenuto un nuovo distintivo."
        assert i18n.tn("badges.banner", 3) == "Hai ottenuto 3 nuovi distintivi."
    with app.test_request_context("/"):
        assert i18n.tn("kanban.mine.count", 1) == "1 ticket assigned to you"
        assert i18n.tn("missing.key", 1) == "missing.key"


# English


def test_the_two_step_summaries_do_not_repeat_the_state():
    assert not EN["auth.two_step.admin_on"].startswith("On ") and not IT["auth.two_step.admin_on"].startswith("Attiva ")
    assert "auth.two_step.settings_help" not in EN


def test_english_permission_hints_describe_the_account_not_the_reader():
    hints = [v for k, v in EN.items() if k.startswith("permission.") and k.endswith(".hint")]
    assert not [text for text in hints if re.search(r"\byou(r)?\b", text)]


def test_the_page_builder_hint_names_its_button_instead_of_a_missing_glyph():
    for text in (EN["page_builder.editor.paste_hint"], IT["page_builder.editor.paste_hint"]):
        assert "⎘" not in text and "⧉" not in text


# Italian


# Italian "wiki" is feminine across the suite (README.it, the site, the hosting portal, the desktop launcher).
MASCULINE_WIKI = re.compile(
    r"\b(?:il|del|al|dal|nel|sul|col|un|questo|quel|nessun|tuo|suo|nostro|vostro|proprio|nuovo|primo|stesso"
    r"|i|dei|ai|dai|nei|sui|tuoi|suoi|questi|quei|nuovi|altri|tutti i) wiki\b"
    r"|l'(?:altro|intero) wiki|\bun altro wiki\b"
    r"|\bwiki (?:è |sono )?(?:collegat[oi]|pront[oi]|configurat[oi]|ospitat[oi]|stesso)\b",
    re.IGNORECASE)


def test_italian_uses_one_gender_for_wiki():
    assert not [key for key, text in IT.items() if MASCULINE_WIKI.search(text)]
    docs = WIKI / "features" / "auth" / "builtin_docs" / "it"
    assert not [path.name for path in docs.glob("*/*.md") if MASCULINE_WIKI.search(path.read_text(encoding="utf-8"))]
    assert IT["site_admin.migration.confirm_label"] == "Ho capito che questo sostituisce tutti i dati di questa wiki."
    assert IT["auth.onboarding.intro"].startswith("La tua wiki è pronta.")


def test_italian_canvas_and_kanban_terms_are_consistent():
    assert IT["permission_group.canvas"] == "Canvas" and IT["bulk_manage.canvases"] == "Canvas"
    assert "lavagn" not in " ".join(IT.values()).lower()
    assert IT["kanban.bulk.toggle"] == "Seleziona"
    assert " ticket" not in IT["api_service.webhooks.intro"]


def test_italian_uses_one_word_for_review_and_editor():
    assert IT["attention.review"] == IT["contributions.review"] == "Esamina"
    assert IT["attention.banner.open"] == "Esamina ora"
    assert {IT[key] for key in EN if key.endswith("role.editor")} == {"Editor"}
    portal = json.loads((ROOT / "hosting/translations/it.json").read_text(encoding="utf-8"))
    assert portal["hosting.wiki_role.editor"] == "Editor" and portal["hosting.attention.review"] == "Esamina"
    assert portal["hosting.dashboard.shared_by"] == "condivisa da {username}"


def test_italian_terminology_is_consistent():
    assert "codice di 6 cifre" not in " ".join(IT.values())
    assert IT["auth.onboarding.mode_easy"] == "Selezione consigliata"
    assert IT["api_service.admin.audit"] == "Registro delle chiamate API"
    assert "registro di audit" not in " ".join(IT.values()).lower()
    assert IT["site_admin.section.access"] != IT["site_admin.section.signin"].split()[0]
    assert "ostruttore" not in " ".join(IT.values())
    assert IT["leaderboard.streak_value"].startswith("{current} in corso")
    assert IT["common.optional"] == "opzionale"
    # The API guide calls a Kanban card "scheda", like the Kanban pages.
    assert not [k for k, v in IT.items() if k.startswith("api_service.endpoint.") and re.search(r"\bticket\b", v)]


# German


def _german_texts() -> dict[str, str]:
    """Every German string of the wiki (catalogues and built-in guide) by key or file."""
    texts = {}
    for path in sorted((WIKI / "features").glob("*/translations/de.json")):
        texts.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if isinstance(v, str)})
    for path in sorted((WIKI / "features" / "auth" / "builtin_docs" / "de").rglob("*.md")):
        texts[path.name] = path.read_text(encoding="utf-8")
    return texts


def test_german_never_reads_as_the_formal_address():
    # The interface says "du" throughout. A capitalised "Ihr"/"Ihre" meaning "its"/"their" at the start of a
    # sentence reads as the formal "your", so it is avoided too.
    formal = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b")
    assert [key for key, value in _german_texts().items() if formal.search(value)] == []


def test_german_quotes_names_with_german_quotation_marks():
    # „…“, as every other German string that quotes a name; straight quotes stay for code only.
    api = feature_catalogue("api_service", "de")
    assert api["api_service.error.token_name_reserved"].startswith("Der Name „{name}“ ")


def test_german_tells_the_reservation_pages_apart():
    # English has "Check-outs" (admin) and "Reservations" (apps), Italian "Check-out" and "Prenotazioni".
    # German needs three distinct labels next to "Seitenschutz und Reservierungen" in the admin menu.
    german = feature_catalogue("page_governance", "de")
    labels = [german[f"page_governance.nav.{key}"] for key in ("checkouts", "directory", "governance")]
    assert len(set(labels)) == 3
    assert german["page_governance.admin.checkouts_title"] == german["page_governance.nav.checkouts"]


# Read aloud languages


def test_tts_language_glosses_exist_in_every_language():
    from bananawiki.wiki.features.tts import text

    en, it, de = (feature_catalogue("tts", lang) for lang in ("en", "it", "de"))
    # Every language has a gloss, except the two called by their own name everywhere.
    keys = {f"tts.lang.{code}" for code in text.SUPPORTED_LANGUAGES} - {"tts.lang.af", "tts.lang.ha"}
    for glosses in (en, it, de):
        assert {k for k in glosses if k.startswith("tts.lang.")} == keys
    assert set(text.NATIVE_NAMES) == set(text.SUPPORTED_LANGUAGES)
    assert it["tts.lang.ar"] == "arabo" and it["tts.lang.de"] == "tedesco"


def test_tts_language_labels_use_the_readers_language(app):
    from bananawiki.wiki.features.tts import text

    with app.test_request_context("/?lang=it"):
        assert text.language_label("ar") == "العربية (arabo)"
        assert text.language_label("it") == "Italiano"
        assert text.language_label("en") == "English (inglese)"
        names = [label for _code, label in text.language_choices(text.SUPPORTED_LANGUAGES)]
        assert names.index("Shqip (albanese)") < names.index("አማርኛ (amarico)") < names.index("Čeština (ceco)")
        assert not [label for label in names if "(Arabic)" in label or "(German)" in label]
    with app.test_request_context("/"):
        assert text.language_label("en") == "English"
        assert text.language_label("de") == "Deutsch (German)"


# Formatting helpers


def test_file_sizes_follow_the_language(app):
    with app.test_request_context("/?lang=it"):
        assert templating.human_bytes(2355) == "2,3 KB"
        assert templating.human_bytes(5 * 1024 * 1024) == "5 MB"
    with app.test_request_context("/"):
        assert templating.human_bytes(2355) == "2.3 KB"
        assert templating.human_bytes(900) == "900 B"


def test_time_ago_has_a_singular_day(app):
    from datetime import timedelta

    from bananawiki.core.timeutil import utcnow

    with app.test_request_context("/?lang=it"):
        assert templating.time_ago(utcnow() - timedelta(hours=30)) == "1 giorno fa"


def test_device_labels_summarise_user_agents():
    agent = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "HeadlessChrome/140.0.0.0 Safari/537.36")
    assert templating.device_label(agent) == "Chrome (headless) · Linux"
    assert templating.device_label("x" * 100).endswith("…")


def test_translate_with_code_escapes_the_sentence(app):
    with app.test_request_context("/"):
        out = templating.translate_with_code("missing.<b>{x}</b>", x="a<b")
    assert str(out) == "missing.&lt;b&gt;<code>a&lt;b</code>&lt;/b&gt;"


def test_capfirst_digits_and_path_breaks(app):
    assert templating.capfirst("archived “x”") == "Archived “x”"
    assert str(templating.break_after_slashes("/api/v1/pages/{slug}<x>")) == (
        "/<wbr>api/<wbr>v1/<wbr>pages/<wbr>{slug}&lt;x&gt;")
    with app.test_request_context("/?lang=it"):
        assert templating.group_digits(2457) == "2.457"
    with app.test_request_context("/"):
        assert templating.group_digits(2457) == "2,457"


def test_legacy_kanban_messages_are_shown_as_stored():
    from bananawiki.wiki.features.kanban import logtext

    assert logtext.render("added the column “Done”") == "added the column “Done”"
    assert len(logtext.message("kanban.log.ticket_created", title="x" * 2000)) <= logtext.LIMIT


def test_audit_details_are_translated():
    template = feature_file("audit", "templates", "audit", "index.html")
    assert "t('audit.detail.' ~ key, key)" in template and "t('feature.' ~ row.target_id ~ '.name'" in template


# Built-in guide


def test_builtin_docs_do_not_repeat_their_title_as_a_heading():
    from bananawiki.wiki.features.auth import docs

    for language in docs.LANGUAGES:
        for title, _slug, content in docs.pages("full", language):
            assert title and not content.startswith("#")
