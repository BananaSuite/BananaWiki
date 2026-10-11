"""Regression tests for the fourth pass of the 1.7 interface review.

Each test pins one fix from the re-check: markup a page renders, a rule the stylesheets or scripts
keep, or a string the catalogues hold in English and Italian.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki.features.pages.search import snippet_html, snippet_text

from .pages_support import make_page, set_feature

ROOT = Path(__file__).resolve().parents[2] / "bananawiki"
CORE_CSS = (ROOT / "core/static/css/bananawiki.css").read_text(encoding="utf-8")
CONTENT_CSS = (ROOT / "wiki/static/css/content.css").read_text(encoding="utf-8")
CORE_JS = (ROOT / "wiki/static/js/bananawiki.js").read_text(encoding="utf-8")
WIKI = ROOT / "wiki"


def feature_file(*parts: str) -> str:
    return (WIKI / "features" / Path(*parts)).read_text(encoding="utf-8")


def catalogue(feature: str, lang: str) -> dict[str, str]:
    path = WIKI / "features" / feature / "translations" / f"{lang}.json"
    return {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"}


# ── Tables ───────────────────────────────────────────────────────────────────


def test_prose_last_column_keeps_whole_words_without_dead_width_on_phones():
    # Ordinary words never split (the minimum fits them; a narrow viewport scrolls the table instead),
    # and on phones a short last column (mentions) does not add the old 12rem of empty width.
    assert ".prose td:last-child { overflow-wrap: anywhere; min-width: 10rem; }" in CONTENT_CSS
    assert "@media (max-width: 600px) { .prose td:last-child { min-width: 8rem; } }" in CONTENT_CSS
    assert "min-width: 12rem" not in CONTENT_CSS


def test_stacking_tables_follow_the_room_they_have():
    # Phones stack by media query; wider screens stack whenever the columns do not fit (bananawiki.js).
    for rule in (".table--stacked thead {", ".table--stacked tr {", ".table--stacked td.actions {",
                 ".table-wrap:has(> .table--stacked) { background: none; }",
                 ".api-endpoints.table--stacked { min-width: 0; table-layout: auto; }"):
        assert rule in CORE_CSS, rule
    assert "BW.fitTables = function" in CORE_JS and "BW.fitTables();" in CORE_JS
    assert 'table.classList.add("table--stacked")' in CORE_JS


def test_a_section_that_starts_with_cards_draws_no_empty_band():
    assert (":is(.section-stack > .card, .section-stack > .stack > .card) > .table-wrap:first-child"
            ":has(> .table--stack) { border-top: 0; }") in CORE_CSS


@pytest.mark.parametrize("parts", [
    ("audit", "templates", "audit", "index.html"),
    ("admin", "templates", "admin", "users.html"),
    ("chat", "templates", "chat", "group_view.html"),
])
def test_more_tables_stack_into_cards(parts):
    template = feature_file(*parts)
    assert "table table--stack" in template and "data-label=" in template


def test_audit_log_renders_one_card_per_entry(app, admin_client):
    set_feature(app, "audit", True)
    html = admin_client.get("/admin/audit").get_data(as_text=True)
    assert '<table class="table table--stack">' in html
    for label in ("By", "Target", "Details", "IP address"):
        assert f'data-label="{label}"' in html


def test_webhook_table_stacks_and_long_addresses_wrap():
    template = feature_file("api_service", "templates", "api_service", "admin.html")
    webhooks = template.split('id="webhooks"', 1)[1].split("</table>", 1)[0]
    assert '<table class="table table--stack">' in webhooks
    assert '<a class="mono break-anywhere"' in webhooks and "hook.url|path_breaks" in webhooks
    assert "data-label=\"{{ t('api_service.webhooks.events') }}\"" in webhooks
    assert ".break-anywhere { overflow-wrap: anywhere; }" in CORE_CSS


# ── Layout ───────────────────────────────────────────────────────────────────


def test_signed_out_visitor_gets_no_empty_sidebar(app, db, client):
    from .api_support import enable_api

    enable_api(app, db)
    html = client.get("/api-docs").get_data(as_text=True)
    assert 'class="shell shell--no-sidebar"' in html
    assert 'id="sidebar"' not in html and "data-sidebar-toggle" not in html
    assert ".shell--no-sidebar { grid-template-columns: minmax(0, 1fr); }" in CORE_CSS


def test_signed_in_pages_keep_the_sidebar(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    assert 'id="sidebar"' in html and "data-sidebar-toggle" in html and "shell--no-sidebar" not in html


def test_page_history_row_panel_is_not_a_menu_with_fields(app, admin_client, admin):
    from bananawiki.wiki.features.pages import service

    from .pages_support import in_app

    page = make_page(app, "Story", "once upon a time", author_id=admin["id"])
    in_app(app, lambda: service.update(service.get(page["id"]), author_id=admin["id"],
                                       content="once upon a rainy time", edit_message="weather"))
    html = admin_client.get("/page/story/history").get_data(as_text=True)
    table = html.split('<table class="table table--stack">', 1)[1].split("</table>", 1)[0]
    panels = re.findall(r'<div class="menu__panel"([^>]*)>(.*?)</details>', table, re.S)
    # The panel holds the transfer forms (a text field each): a labelled group, never a role="menu".
    assert panels and any("<input id=" in body for _attrs, body in panels)
    for attrs, body in panels:
        assert 'role="group"' in attrs and 'role="menu' not in attrs + body
    template = feature_file("page_history", "templates", "page_history", "list.html")
    assert 'role="menu"' not in template and 'role="menuitem"' not in template
    assert ".menu__panel .menu__form, .menu__form { display: grid;" in feature_file("pages", "static", "pages.css")


def test_tour_mock_labels_wrap_instead_of_being_cut():
    css = feature_file("auth", "static", "auth.css")
    item = css.split(".tour-mock__item {", 1)[1].split("}", 1)[0]
    assert "nowrap" not in item and "ellipsis" not in item
    assert "@media (max-width: 30rem) { .tour-mock__sidebar { width: 50%; } }" in css


def test_kanban_board_starts_at_its_first_column_and_the_list_sits_under_the_header():
    css = feature_file("kanban", "static", "kanban.css")
    assert re.search(r"\.kanban-board \{[^}]*scroll-padding-inline: \.1rem", css)
    template = feature_file("kanban", "templates", "kanban", "list.html")
    after_header = template.split("</header>", 1)[1]
    assert after_header.index('<ul class="kanban-boards"') < after_header.index('id="kanban-list-live"')
    # A snap point a couple of pixels inside the edge is not hidden content.
    assert "if (left > 2) more.push(\"left\");" in CORE_JS


def test_session_sign_out_stays_beside_the_session():
    css = feature_file("users", "static", "users.css")
    assert re.search(r"\.users-session__main \{ flex: 1 1 0;", css)
    assert ".users-session > :not(.users-session__main) { flex: none; }" in css


# ── Read aloud languages ─────────────────────────────────────────────────────


def test_tts_language_glosses_exist_in_both_languages():
    from bananawiki.wiki.features.tts import text

    en, it = catalogue("tts", "en"), catalogue("tts", "it")
    # Every language has a gloss in both catalogues, except the two called by their own name in both.
    keys = {f"tts.lang.{code}" for code in text.SUPPORTED_LANGUAGES} - {"tts.lang.af", "tts.lang.ha"}
    assert {k for k in en if k.startswith("tts.lang.")} == keys == {k for k in it if k.startswith("tts.lang.")}
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


# ── Search snippets ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    ("**Operations**, **Product**, HR", "Operations, Product, HR"),
    ("[TOC] Overview of the release", "Overview of the release"),
    (" … release notes and project pages. ![Team offsite 2026](/static/up … ", "… release notes and project pages. …"),
    ('see [[kanban board="1"]] here', "see here"),
    ("ask @mia_member about snake_case_name", "ask @mia_member about snake_case_name"),
    ("read [the docs](https://example.org/docs) now", "read the docs now"),
    ("## Heading\n> quote\n- [x] done\n| a | b |\n| --- | --- |", "Heading quote done a b"),
    (" … example.org/x.png) after the image", "… after the image"),
])
def test_search_snippets_show_words_not_markdown(raw, expected):
    assert snippet_text(raw) == expected


def test_search_snippet_keeps_the_match_marks_and_escapes():
    html = str(snippet_html(" … the _\x02release\x03_ <b>notes</b> for mia_\x02member\x03 … ", [], marked=True))
    assert html == "… the <mark>release</mark> &lt;b&gt;notes&lt;/b&gt; for mia_<mark>member</mark> …"


# ── German ───────────────────────────────────────────────────────────────────


def test_long_german_compounds_are_hyphenated_where_they_must_break():
    # "Kontozusammenführungen" in the admin sidebar, "Reservierungskontingent" in a phone's page heading and
    # "Wiedergabegeschwindigkeiten" in the read-aloud system table are wider than their column: they broke at
    # any letter, without a hyphen. German pages hyphenate them; English and Italian words keep wrapping whole,
    # and the voice folder (a path) is never given hyphens that are not in it.
    admin_css = feature_file("admin", "static", "admin.css")
    tts_css = feature_file("tts", "static", "tts.css")
    assert ".admin-nav .nav-list .nav-label { white-space: normal; overflow: visible; overflow-wrap: anywhere; }" \
        in admin_css
    assert ":lang(de) .admin-nav .nav-list .nav-label { hyphens: auto; }" in admin_css
    # A balanced heading split "Reservierungen", which fits a phone's line, as "Reservierun-gen verwalten".
    assert ":lang(de) .page-header h1 { hyphens: auto; hyphenate-limit-chars: 18 6 4; }" in CORE_CSS
    assert ".tts-system td { overflow-wrap: anywhere; }" in tts_css
    assert ":lang(de) .tts-system td:not(.mono) { hyphens: auto; }" in tts_css
    template = feature_file("tts", "templates", "tts", "admin.html")
    assert '<td class="mono small">{{ system.voice_dir }}</td>' in template
    for css in (admin_css, tts_css, CORE_CSS):
        selectors = re.findall(r"([^{}]*)\{[^}]*hyphens: auto", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        assert selectors and all(item.strip().startswith(":lang(de) ") for item in selectors), "only German"


def _german_texts() -> dict[str, str]:
    """Every German string of the wiki (catalogues and built-in guide) by key or file."""
    texts = {}
    for path in sorted((WIKI / "features").glob("*/translations/de.json")):
        texts.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if isinstance(v, str)})
    for path in sorted((WIKI / "features" / "auth" / "builtin_docs" / "de").rglob("*.md")):
        texts[path.name] = path.read_text(encoding="utf-8")
    return texts


def test_german_never_reads_as_the_formal_address():
    # The interface says "du" throughout: a capitalised "Ihr"/"Ihre" meaning "its"/"their" at the start of a
    # sentence reads as the formal "your" (chat.clear.dm_confirm, plugin_manager.lock.owner_only did).
    formal = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b")
    assert [key for key, value in _german_texts().items() if formal.search(value)] == []


def test_german_quotes_names_with_german_quotation_marks():
    # „…“, as every other German string that quotes a name; straight quotes stay for code only.
    api = catalogue("api_service", "de")
    assert api["api_service.error.token_name_reserved"].startswith("Der Name „{name}“ ")


def test_german_tells_the_reservation_pages_apart():
    # English has "Check-outs" (admin) and "Reservations" (apps), Italian "Check-out" and "Prenotazioni":
    # German labelled both "Reservierungen", next to "Seitenschutz und Reservierungen" in the admin menu.
    german = catalogue("page_governance", "de")
    labels = [german[f"page_governance.nav.{key}"] for key in ("checkouts", "directory", "governance")]
    assert len(set(labels)) == 3
    assert german["page_governance.admin.checkouts_title"] == german["page_governance.nav.checkouts"]
