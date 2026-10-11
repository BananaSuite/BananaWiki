"""Regression tests for the 1.7 interface review (layout, accessibility and wording fixes).

Each test pins one fix that a visual or automated (axe) review reported: the markup a page
renders, a rule the stylesheet must keep, or a string the catalogues must hold in English and
Italian.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki import i18n, templating
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.markdown import render, to_plain_text

from .chat_support import send, start_dm
from .pages_support import make_category, make_page

ROOT = Path(__file__).resolve().parents[2] / "bananawiki"
CORE_CSS = (ROOT / "core/static/css/bananawiki.css").read_text(encoding="utf-8")
WIKI = ROOT / "wiki"


def catalogue(lang: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(WIKI.glob(f"**/translations/{lang}.json")):
        if "examples" in path.parts:
            continue
        merged.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"})
    return merged


EN, IT = catalogue("en"), catalogue("it")


# ── Catalogues ───────────────────────────────────────────────────────────────


def test_every_feature_catalogue_has_the_same_keys_and_placeholders_in_both_languages():
    placeholder = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for english in WIKI.glob("**/translations/en.json"):
        en = json.loads(english.read_text(encoding="utf-8"))
        it = json.loads(english.with_name("it.json").read_text(encoding="utf-8"))
        assert en.keys() == it.keys(), english
        for key, text in en.items():
            if isinstance(text, str):
                assert set(placeholder.findall(text)) == set(placeholder.findall(it[key])), key


def test_english_strings_have_no_plural_hacks():
    # "1 account(s)" read like a template; singular forms now live in "<key>_one".
    assert not [key for key, text in EN.items() if re.search(r"[a-z]\(s\)", text.replace("http(s)", ""))]


@pytest.mark.parametrize("key", [
    "badges.banner", "kanban.mine.count", "contributions.notice.waiting", "contributions.usage",
    "assessments.points_awarded", "assessments.question_count", "chat.groups.member_count", "chat.unread",
    "canvas.history.nodes", "canvas.history.edges", "time.days_ago", "time.months_ago", "time.years_ago",
    "bulk_manage.page_count", "admin.users.count", "js.kanban.tickets_count", "users.profile.contributions_in",
])
def test_counted_strings_have_a_singular_in_both_languages(key):
    assert key + "_one" in EN and key + "_one" in IT


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


def test_the_page_builder_hint_names_its_button_instead_of_a_missing_glyph():
    for text in (EN["page_builder.editor.paste_hint"], IT["page_builder.editor.paste_hint"]):
        assert "⎘" not in text and "⧉" not in text


def test_tn_picks_the_singular_only_when_the_language_has_one(app):
    with app.test_request_context("/?lang=it"):
        assert i18n.tn("badges.banner", 1) == "Hai ottenuto un nuovo distintivo."
        assert i18n.tn("badges.banner", 3) == "Hai ottenuto 3 nuovi distintivi."
    with app.test_request_context("/"):
        assert i18n.tn("kanban.mine.count", 1) == "1 ticket assigned to you"
        assert i18n.tn("missing.key", 1) == "missing.key"


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


# ── Markdown ─────────────────────────────────────────────────────────────────


def test_plain_text_keeps_punctuation_next_to_inline_markup():
    assert to_plain_text("before **v1.7.0**.") == "before v1.7.0."
    assert to_plain_text("See [the docs](https://x.y), then *go*!\n\nNext") == "See the docs, then go! Next"


def test_a_list_nested_by_three_spaces_under_a_numbered_item_nests():
    html = render("1. a\n2. b\n   - c\n")
    assert re.search(r"<li>b\s*<ul>\s*<li>c</li>", html)


def test_builtin_docs_do_not_repeat_their_title_as_a_heading():
    from bananawiki.wiki.features.auth import docs

    for language in docs.LANGUAGES:
        for title, _slug, content in docs.pages("full", language):
            assert title and not content.startswith("#")


# ── Stylesheet rules ─────────────────────────────────────────────────────────


def test_closed_phone_drawer_is_hidden_from_focus_and_paint():
    narrow = CORE_CSS.split("@media (max-width: 900px)", 1)[1].split("\n}\n", 1)[0]
    assert "visibility: hidden" in narrow and "box-shadow: none" in narrow
    assert re.search(r"body\.sidebar-open \.sidebar \{[^}]*visibility: visible", narrow)
    assert "var(--drawer-top" in narrow


def test_checkboxes_do_not_shrink_beside_a_wrapping_label():
    assert re.search(r"\.check input \{ flex: none;", CORE_CSS)


def test_tables_show_scroll_shadows_and_stack_on_phones():
    assert "no-repeat local" in CORE_CSS
    assert ".table--stack td[data-label]" in CORE_CSS


def test_links_in_running_text_are_underlined():
    assert ".page-header__meta a:not(.btn) { text-decoration: underline" in CORE_CSS
    attachments = (WIKI / "features/attachments/static/attachments.css").read_text(encoding="utf-8")
    assert ".attachments__item > a { text-decoration: underline" in attachments


def test_announcement_and_difficulty_colours_mix_more_text_colour_in_the_light_theme():
    announcements = (WIKI / "features/announcements/static/announcements.css").read_text(encoding="utf-8")
    assert '[data-theme="light"] :is(.announcement, .announcement-full) { --announce-mix: 45%; }' in announcements
    assert "opacity" not in announcements.split(".announcement__counter", 1)[1].split("}", 1)[0]
    tags = (WIKI / "features/difficulty_tags/static/tags.css").read_text(encoding="utf-8")
    assert '[data-theme="light"] .difficulty-tag { --tag-mix: 45%; }' in tags


def test_native_progress_bar_is_styled():
    assert "progress.progress::-webkit-progress-value" in CORE_CSS


# ── Rendered pages ───────────────────────────────────────────────────────────


def test_chat_log_keeps_list_semantics_inside_the_live_region(app, client, login, make_user):
    alice, _bob = make_user("alice"), make_user("bob")
    login(client, alice)
    dm = start_dm(client, "bob")
    send(client, f"/chats/{dm}/send", "hello")
    html = client.get(f"/chats/{dm}").get_data(as_text=True)
    assert re.search(r'<div class="chat__region" role="log"[^>]*>\s*<ol class="chat__log" id="chat-log" tabindex="0">', html)
    assert 'class="chat-msg__actions"' in html or 'chat-msg__report' not in html


def test_blocking_clears_the_unread_badge(app, client, login, make_user, db):
    alice, bob = make_user("alice"), make_user("bob")
    other = app.test_client()
    login(other, bob)
    dm = start_dm(other, "alice")
    send(other, f"/chats/{dm}/send", "spam")
    login(client, alice)
    assert client.post("/chats/blocks", data={"user_id": bob["id"], "next": "/chats"}).status_code in (302, 303)
    assert db.scalar("SELECT unread_count_user1 + unread_count_user2 FROM chats WHERE id = ?", (dm,)) == 0


def test_kanban_board_region_is_focusable_and_history_is_translated(app, admin_client, db):
    response = admin_client.post("/kanban/create", data={"title": "Release", "description": ""})
    board_id = int(response.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    html = admin_client.get(f"/kanban/{board_id}").get_data(as_text=True)
    assert 'id="kanban-board" role="region" tabindex="0"' in html
    stored = db.scalar("SELECT edit_message FROM kanban_board_history WHERE board_id = ?", (board_id,))
    assert stored.startswith('{"k":"kanban.log.board_created"')
    italian = admin_client.get(f"/kanban/{board_id}/history?lang=it").get_data(as_text=True)
    # The Summary column starts with a capital, like the canvas history (capfirst).
    assert "Ha creato la bacheca" in italian and "reated the board" not in italian


def test_legacy_kanban_messages_are_shown_as_stored():
    from bananawiki.wiki.features.kanban import logtext

    assert logtext.render("added the column “Done”") == "added the column “Done”"
    assert len(logtext.message("kanban.log.ticket_created", title="x" * 2000)) <= logtext.LIMIT


def test_contribution_diff_is_a_labelled_group_without_blank_lines(app_factory, make_user, login):
    from bananawiki.wiki.features.contributions import service
    from bananawiki.wiki.features.pages import service as pages

    from .governance_support import as_user, build_app

    application = build_app(app_factory, "contributions")
    reader, boss = make_user("reader"), make_user("boss", role="admin")
    with application.test_request_context(), connection_scope():
        page = pages.create("Article", "one\ntwo\nthree\n", author_id=None)
        as_user(reader)
        contribution_id = service.propose(page, reader, title="Article", content="one\nTWO\nthree\n", reason="fix")
    client = application.test_client()
    login(client, boss)
    html = client.get(f"/admin/contributions/{contribution_id}").get_data(as_text=True)
    assert 'role="table"' not in html
    block = html.split('<div class="diff" role="group"', 1)[1].split("</section>", 1)[0]
    # The container keeps line breaks (white-space: pre-wrap): no newlines between its rows.
    assert re.match(r'[^>]*><div', block) and not re.search(r"</div>\s+<div", block)
    # The review list opens inside the administration layout (side menu) for an administrator.
    assert 'class="admin-nav"' in client.get("/admin/contributions").get_data(as_text=True)


def test_custom_pages_admin_list_keeps_the_footer_outside_the_admin_grid(admin_client, db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    html = admin_client.get("/admin/custom-pages").get_data(as_text=True)
    assert "</div>" not in html.split("</head>", 1)[0]
    # The footer follows the administration grid instead of becoming one of its columns.
    after_shell = html.split('class="admin-shell', 1)[1]
    depth, position = 1, after_shell.index(">") + 1
    for match in re.finditer(r"<(/?)div\b", after_shell[position:]):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            assert "site-footer" in after_shell[position + match.end():]
            assert "site-footer" not in after_shell[:position + match.start()]
            break


def test_navigation_page_offers_move_buttons_for_pages_and_marks_the_ends(app, admin_client):
    category = make_category(app, "Handbook")
    make_page(app, "First", "x", category_id=category["id"])
    make_page(app, "Second", "x", category_id=category["id"])
    html = admin_client.get("/navigation").get_data(as_text=True)
    # Pages, not only categories, can be moved with the keyboard (drag and drop needs a mouse).
    page_items = re.findall(r'<li data-reorder-item data-id="\d+" draggable="true">\s*'
                            r'<span class="nav-tree__actions"><span class="reorder-buttons">', html)
    assert len(page_items) >= 2
    navigation = (WIKI / "features/pages/static/navigation.js").read_text(encoding="utf-8")
    assert "BW.markMoveEnds(list" in navigation and ".scrollIntoView(" not in navigation


def test_uncategorised_listing_is_styled_like_a_category(app, admin_client):
    make_page(app, "Loose", "x")
    html = admin_client.get("/navigation/pages?category_id=0").get_data(as_text=True)
    assert 'class="breadcrumbs"' in html and 'class="page-list"' in html and 'class="page-header"' in html


def test_empty_builder_page_says_so(admin_client, db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1")
    admin_client.post("/admin/custom-pages/create", content_type="multipart/form-data",
                      data={"path": "/landing", "title": "Landing", "content_type": "builder", "is_published": "1"})
    html = admin_client.get("/landing").get_data(as_text=True)
    assert 'class="empty-state"' in html


def test_tour_progress_has_a_name_and_no_fullwidth_plus(app, admin_client):
    html = admin_client.get("/tour/step/4?role=editor").get_data(as_text=True)
    assert re.search(r'<progress class="progress"[^>]*aria-label="', html)
    assert "＋" not in html


# ── Second pass (1.7 interface review) ───────────────────────────────────────


def test_api_guide_sets_parameter_names_as_code(app, db, client):
    from .api_support import enable_api

    enable_api(app, db)
    html = client.get("/api-docs?lang=it").get_data(as_text=True)
    assert "<code>X-RateLimit-Reset</code>" in html and "<code>Idempotency-Key</code>" in html
    conventions = html.split('<p class="api-conventions">', 1)[1].split("</p>", 1)[0]
    assert conventions.startswith("Gli elenchi accettano <code>limit</code>") and "{" not in conventions


def test_translate_with_code_escapes_the_sentence(app):
    with app.test_request_context("/"):
        out = templating.translate_with_code("missing.<b>{x}</b>", x="a<b")
    assert str(out) == "missing.&lt;b&gt;<code>a&lt;b</code>&lt;/b&gt;"


def test_profile_field_rows_put_each_control_in_its_column(admin_client):
    admin_client.post("/admin/profile-fields", data={"action": "create", "label": "Team", "field_type": "text"})
    html = admin_client.get("/admin/profile-fields").get_data(as_text=True)
    row = html.split("<tbody>", 1)[1].split("</tr>", 1)[0]
    assert row.count("<td") == 3 and 'colspan' not in row
    assert re.search(r'<select id="type-\d+" name="field_type" form="pf-\d+"', row)
    assert re.search(r'<button class="btn btn--small" form="pf-\d+" name="action" value="update"', row)


def test_user_detail_names_the_chat_state_in_italian(admin_client, make_user):
    bob = make_user("bob")
    html = admin_client.get(f"/admin/users/{bob['id']}?lang=it").get_data(as_text=True)
    assert "<dd>Consentita</dd>" in html
    # Administrators set another person's password: the hint names the account, not "your" username.
    assert IT["auth.hint.password_admin"] in html.replace("&#39;", "'")


def test_hidden_sidebar_pages_have_a_single_title(app, admin_client):
    make_page(app, "Draft notes", "x")
    with app.test_request_context():
        from flask import render_template_string

        html = render_template_string(
            '{% with pages=pages, more=none, category_id=none, current_slug=none, reorder=false %}'
            '{% include "pages/_nav_pages.html" %}{% endwith %}',
            pages=[{"id": 1, "slug": "draft-notes", "title": "Draft notes", "is_deindexed": 1, "pending_deletion": 0}],
            manage_order=False)
    link = html.split("<a ", 1)[1].split(">", 1)[0]
    assert link.count("title=") == 1 and 'class="nav-link--hidden"' in link


def test_federation_empty_states_use_the_card_pattern():
    for name in ("index.html", "sharing.html"):
        template = (WIKI / "features/federation/templates/federation" / name).read_text(encoding="utf-8")
        assert '<div class="empty-state">' not in template and 'class="card empty-state"' in template


def test_video_embeds_do_not_repeat_fullscreen_permission():
    # "allow" already grants fullscreen; a second "allowfullscreen" only makes Chromium warn in the console.
    html = render('[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]\n')
    assert 'allow="fullscreen; picture-in-picture"' in html and "allowfullscreen" not in html
    template = (WIKI / "features/custom_pages/templates/custom_pages/youtube.html").read_text(encoding="utf-8")
    assert "allowfullscreen" not in template and "fullscreen;" in template
