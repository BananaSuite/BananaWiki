"""Interface layout: the markup pages render and the rules the stylesheets and scripts keep."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.markdown import render

from .chat_support import JSON, send, start_dm, switch
from .pages_support import make_category, make_page, set_feature

ROOT = Path(__file__).resolve().parents[2] / "bananawiki"
CORE_CSS = (ROOT / "core/static/css/bananawiki.css").read_text(encoding="utf-8")
CONTENT_CSS = (ROOT / "wiki/static/css/content.css").read_text(encoding="utf-8")
CORE_JS = (ROOT / "wiki/static/js/bananawiki.js").read_text(encoding="utf-8")
WIKI = ROOT / "wiki"


def catalogue(lang: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(WIKI.glob(f"**/translations/{lang}.json")):
        if "examples" in path.parts:
            continue
        merged.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"})
    return merged


def feature_file(*parts: str) -> str:
    return (WIKI / "features" / Path(*parts)).read_text(encoding="utf-8")


EN, IT = catalogue("en"), catalogue("it")


# Stylesheets


def test_closed_phone_drawer_is_hidden_from_focus_and_paint():
    narrow = CORE_CSS.split("@media (max-width: 900px)", 1)[1].split("\n}\n", 1)[0]
    assert "visibility: hidden" in narrow and "box-shadow: none" in narrow
    assert re.search(r"body\.sidebar-open \.sidebar \{[^}]*visibility: visible", narrow)
    assert "var(--drawer-top" in narrow


def test_checkboxes_do_not_shrink_beside_a_wrapping_label():
    assert re.search(r"\.check input \{ flex: none;", CORE_CSS)


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


def test_links_in_announcements_are_underlined_in_the_announcement_colour():
    # The global rule drops the underline and the bar's links take the text colour, so nothing else marks them.
    announcements = (WIKI / "features/announcements/static/announcements.css").read_text(encoding="utf-8")
    assert (".announcement a, .announcement-full .content a { color: inherit; text-decoration: underline;"
            in announcements)


def test_native_progress_bar_is_styled():
    assert "progress.progress::-webkit-progress-value" in CORE_CSS


def test_shared_layout_rules():
    assert "html:has(.form-actions--sticky) { scroll-padding-bottom" in CORE_CSS
    assert ".alert__body > :last-child { margin-bottom: 0; }" in CORE_CSS
    assert re.search(r"\.stat \{[^}]*align-content: start", CORE_CSS)
    assert ":is(input, textarea, select).mono { font-family: var(--font-mono); }" in CORE_CSS
    assert "grid-template-rows: subgrid" in CORE_CSS
    assert re.search(r"\.menu__panel a, \.menu__panel button \{[^}]*justify-content: flex-start", CORE_CSS)
    # Tabs wrap on phones instead of scrolling past the edge with no cue.
    assert "flex-wrap: nowrap; overflow-x: auto" not in CORE_CSS
    assert "[data-scroll-cue]:focus-within" in CORE_CSS


# Page content


def test_task_boxes_sit_on_the_content_edge():
    assert re.search(r"\.prose \.task-box \{[^}]*text-indent: 0", CONTENT_CSS)
    html = render("- [x] done\n- [ ] open\n")
    assert '<span class="task-box">☑</span>done' in html and 'aria-hidden' not in html


def test_prose_cells_keep_a_date_on_one_line_and_code_blocks_show_scroll_shadows():
    assert re.search(r"\.prose th, \.prose td \{[^}]*box-sizing: content-box", CONTENT_CSS)
    assert '<td><span class="nowrap">2026-09-01</span></td>' in render("| Start |\n| --- |\n| 2026-09-01 |\n")
    assert "background:" in CONTENT_CSS.split(".prose pre {", 1)[1].split("}", 1)[0]


def test_prose_last_column_keeps_whole_words_without_dead_width_on_phones():
    # Ordinary words never split (the minimum fits them; a narrow viewport scrolls the table instead),
    # and on phones a short last column (mentions) keeps a smaller minimum.
    assert ".prose td:last-child { overflow-wrap: anywhere; min-width: 10rem; }" in CONTENT_CSS
    assert "@media (max-width: 600px) { .prose td:last-child { min-width: 8rem; } }" in CONTENT_CSS
    assert "min-width: 12rem" not in CONTENT_CSS


def test_video_embeds_do_not_repeat_fullscreen_permission():
    # "allow" already grants fullscreen; a second "allowfullscreen" only makes Chromium warn in the console.
    html = render('[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"]]\n')
    assert 'allow="fullscreen; picture-in-picture"' in html and "allowfullscreen" not in html
    template = (WIKI / "features/custom_pages/templates/custom_pages/youtube.html").read_text(encoding="utf-8")
    assert "allowfullscreen" not in template and "fullscreen;" in template


# Tables


def test_tables_show_scroll_shadows_and_stack_on_phones():
    assert "no-repeat local" in CORE_CSS
    assert ".table--stack td[data-label]" in CORE_CSS


def test_short_action_tables_stack_on_phones():
    for parts in (("drafts", "templates", "drafts", "mine.html"),
                  ("kanban", "templates", "kanban", "history.html"),
                  ("canvas", "templates", "canvas", "history.html"),
                  ("page_history", "templates", "page_history", "list.html"),
                  ("contributions", "templates", "contributions", "review_list.html"),
                  ("contributions", "templates", "contributions", "mine.html"),
                  ("page_governance", "templates", "page_governance", "admin_governance.html"),
                  ("custom_pages", "templates", "custom_pages", "admin_list.html"),
                  ("assessments", "templates", "assessments", "admin.html"),
                  ("assessments", "templates", "assessments", "points.html"),
                  ("admin", "templates", "admin", "roles.html"),
                  ("admin", "templates", "admin", "_codes_table.html"),
                  ("admin", "templates", "admin", "_sessions_table.html"),
                  ("badges", "templates", "badges", "admin_list.html"),
                  ("temporary_accounts", "templates", "temporary_accounts", "admin.html"),
                  ("chat", "templates", "chat", "admin_groups.html"),
                  ("chat", "templates", "chat", "admin_chats.html"),
                  ("api_service", "templates", "api_service", "docs.html")):
        template = feature_file(*parts)
        assert "table--stack" in template and "data-label=" in template, parts


@pytest.mark.parametrize("parts", [
    ("audit", "templates", "audit", "index.html"),
    ("admin", "templates", "admin", "users.html"),
    ("chat", "templates", "chat", "group_view.html"),
])
def test_long_tables_stack_into_cards(parts):
    template = feature_file(*parts)
    assert "table table--stack" in template and "data-label=" in template


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


def test_api_guide_paths_break_after_slashes(app, db, client):
    from .api_support import enable_api

    enable_api(app, db)
    html = client.get("/api-docs").get_data(as_text=True)
    assert '<td class="api-endpoints__path"><code>/<wbr>api/<wbr>v1/<wbr>' in html


# Long words


def test_long_german_compounds_are_hyphenated_where_they_must_break():
    # "Kontozusammenführungen" in the admin sidebar, "Reservierungskontingent" in a phone's page heading and
    # "Wiedergabegeschwindigkeiten" in the read-aloud system table are wider than their column. German pages
    # hyphenate them; English and Italian words keep wrapping whole, and the voice folder (a path) never gets
    # hyphens that are not in it.
    admin_css = feature_file("admin", "static", "admin.css")
    tts_css = feature_file("tts", "static", "tts.css")
    assert ".admin-nav .nav-list .nav-label { white-space: normal; overflow: visible; overflow-wrap: anywhere; }" \
        in admin_css
    assert ":lang(de) .admin-nav .nav-list .nav-label { hyphens: auto; }" in admin_css
    # Words that fit a phone's line, such as "Reservierungen", are not split.
    assert ":lang(de) .page-header h1 { hyphens: auto; hyphenate-limit-chars: 18 6 4; }" in CORE_CSS
    assert ".tts-system td { overflow-wrap: anywhere; }" in tts_css
    assert ":lang(de) .tts-system td:not(.mono) { hyphens: auto; }" in tts_css
    template = feature_file("tts", "templates", "tts", "admin.html")
    assert '<td class="mono small">{{ system.voice_dir }}</td>' in template
    for css in (admin_css, tts_css, CORE_CSS):
        selectors = re.findall(r"([^{}]*)\{[^}]*hyphens: auto", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
        assert selectors and all(item.strip().startswith(":lang(de) ") for item in selectors), "only German"


def test_long_labels_stay_inside_stat_cards_and_voice_rows_with_large_text():
    # With the largest text size and letter spacing, labels such as "Fehlgeschlagen" and the voice state
    # "wird bei der ersten Nutzung heruntergeladen" wrap inside their card or row instead of widening the page.
    assert ".stat__label { font-size: var(--font-size-sm); color: var(--muted); overflow-wrap: anywhere; }" in CORE_CSS
    assert ":lang(de) .stat__label { hyphens: auto; }" in CORE_CSS
    assert ".tts-voices .badge { white-space: normal; max-width: 100%; }" in feature_file("tts", "static", "tts.css")


# Shell and navigation


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


def test_most_specific_admin_link_is_the_current_one(admin_client):
    html = admin_client.get("/admin/users/merge").get_data(as_text=True)
    menu = html.split('class="admin-nav"', 1)[1].split("</nav>", 1)[0]
    assert menu.count('aria-current="page"') == 1


def test_leaderboard_link_is_not_offered_to_signed_out_visitors():
    source = (WIKI / "features/leaderboard/__init__.py").read_text(encoding="utf-8")
    assert "visible=lambda user: bool(user)" in source


def test_tour_progress_has_a_name_and_no_fullwidth_plus(app, admin_client):
    html = admin_client.get("/tour/step/4?role=editor").get_data(as_text=True)
    assert re.search(r'<progress class="progress"[^>]*aria-label="', html)
    assert "＋" not in html


def test_tour_mock_labels_wrap_instead_of_being_cut():
    css = feature_file("auth", "static", "auth.css")
    item = css.split(".tour-mock__item {", 1)[1].split("}", 1)[0]
    assert "nowrap" not in item and "ellipsis" not in item
    assert "@media (max-width: 30rem) { .tour-mock__sidebar { width: 50%; } }" in css


# Pages and categories


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


def test_new_category_parent_select_shows_full_paths(app, admin_client):
    parent = make_category(app, "Handbook")
    make_category(app, "Engineering", parent["id"])
    html = admin_client.get("/navigation").get_data(as_text=True)
    assert ">Handbook / Engineering</option>" in html


def test_category_page_lists_pages_before_subcategories(app, admin_client):
    parent = make_category(app, "Handbook")
    make_category(app, "Engineering", parent["id"])
    make_page(app, "Glossary", "x", category_id=parent["id"])
    html = admin_client.get(f"/category/{parent['id']}").get_data(as_text=True)
    body = html.split('class="page-header"', 1)[1]
    assert body.index("Glossary") < body.index("Engineering")


def test_uncategorised_listing_is_styled_like_a_category(app, admin_client):
    make_page(app, "Loose", "x")
    html = admin_client.get("/navigation/pages?category_id=0").get_data(as_text=True)
    assert 'class="breadcrumbs"' in html and 'class="page-list"' in html and 'class="page-header"' in html


def test_uncategorised_listing_ends_its_breadcrumb_and_offers_a_new_page(app, admin_client):
    make_page(app, "Loose", "x")
    html = admin_client.get("/navigation/pages?category_id=0").get_data(as_text=True)
    assert re.search(r'<li aria-current="page">[^<]+</li>\s*</ol>', html)
    assert re.search(r'class="page-header__actions"><a class="btn btn--primary btn--small" href="/create', html)
    # The sidebar marks "Uncategorised" as the current place.
    assert re.search(r'<summary aria-current="page"><span class="nav-label">', html)


def test_page_meta_keeps_the_separator_with_the_history_link():
    template = feature_file("pages", "templates", "pages", "page.html")
    assert '<span class="nowrap">· <a href="{{ url_for(\'pages.history\'' in template


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


def test_empty_builder_page_says_so(admin_client, db):
    db.execute("UPDATE plugins SET enabled = 1 WHERE id = 'custom_pages'")
    db.execute("UPDATE site_settings SET page_builder_enabled = 1")
    admin_client.post("/admin/custom-pages/create", content_type="multipart/form-data",
                      data={"path": "/landing", "title": "Landing", "content_type": "builder", "is_published": "1"})
    html = admin_client.get("/landing").get_data(as_text=True)
    assert 'class="empty-state"' in html


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


# Contributions


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


def test_contribution_edit_page_says_it_edits_a_waiting_proposal(app_factory, make_user, login):
    from bananawiki.wiki.features.contributions import service
    from bananawiki.wiki.features.pages import service as pages

    from .governance_support import as_user, build_app

    application = build_app(app_factory, "contributions")
    reader = make_user("reader")
    with application.test_request_context(), connection_scope():
        page = pages.create("Article", "one\n", author_id=None)
        as_user(reader)
        contribution_id = service.propose(page, reader, title="Article", content="two\n", reason="fix")
    client = application.test_client()
    login(client, reader)
    html = client.get(f"/page/{page['slug']}/contribution/{contribution_id}/edit").get_data(as_text=True)
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]
    heading = html.split("<h1", 1)[1].split("</h1>", 1)[0]
    assert "Edit your proposal for" in title and "Edit your proposal for" in heading
    assert "Propose an edit to" not in html
    assert "is waiting for review" in html


# Chat


def test_chat_log_keeps_list_semantics_inside_the_live_region(app, client, login, make_user):
    alice, _bob = make_user("alice"), make_user("bob")
    login(client, alice)
    dm = start_dm(client, "bob")
    send(client, f"/chats/{dm}/send", "hello")
    html = client.get(f"/chats/{dm}").get_data(as_text=True)
    assert re.search(r'<div class="chat__region" role="log"[^>]*>\s*<ol class="chat__log" id="chat-log" tabindex="0">', html)
    assert 'class="chat-msg__actions"' in html or 'chat-msg__report' not in html


def test_open_chat_reports_count_in_the_attention_total(app, client, login, make_user, admin, db):
    from bananawiki.wiki import attention

    alice, bob = make_user("alice"), make_user("bob")
    login(client, bob)
    dm = start_dm(client, "alice")
    send(client, f"/chats/{dm}/send", "something abusive")
    message = db.scalar("SELECT id FROM chat_messages WHERE content = 'something abusive'")
    switch(client, login, alice)
    assert client.post(f"/chats/{dm}/report", data={"message_id": message}, headers=JSON).status_code == 200
    with app.test_request_context(), connection_scope():
        assert attention.counts(admin).get("chat.reports") == 1
    # The DM list marks a blocked person and does not repeat their messages.
    client.post("/chats/blocks", data={"user_id": bob["id"], "next": "/chats"})
    listing = client.get("/chats").get_data(as_text=True)
    assert EN["chat.blocks.preview_hidden"] in listing and "something abusive" not in listing


# Kanban


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


def test_kanban_board_starts_at_its_first_column_and_the_list_sits_under_the_header():
    css = feature_file("kanban", "static", "kanban.css")
    assert re.search(r"\.kanban-board \{[^}]*scroll-padding-inline: \.1rem", css)
    template = feature_file("kanban", "templates", "kanban", "list.html")
    after_header = template.split("</header>", 1)[1]
    assert after_header.index('<ul class="kanban-boards"') < after_header.index('id="kanban-list-live"')
    # A snap point a couple of pixels inside the edge is not hidden content.
    assert "if (left > 2) more.push(\"left\");" in CORE_JS


# API guide


def test_api_guide_sets_parameter_names_as_code(app, db, client):
    from .api_support import enable_api

    enable_api(app, db)
    html = client.get("/api-docs?lang=it").get_data(as_text=True)
    assert "<code>X-RateLimit-Reset</code>" in html and "<code>Idempotency-Key</code>" in html
    conventions = html.split('<p class="api-conventions">', 1)[1].split("</p>", 1)[0]
    assert conventions.startswith("Gli elenchi accettano <code>limit</code>") and "{" not in conventions


# Accounts and administration


def test_session_sign_out_stays_beside_the_session():
    css = feature_file("users", "static", "users.css")
    assert re.search(r"\.users-session__main \{ flex: 1 1 0;", css)
    assert ".users-session > :not(.users-session__main) { flex: none; }" in css


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


# Federation


def test_federation_empty_states_use_the_plain_pattern():
    for name in ("index.html", "sharing.html"):
        template = (WIKI / "features/federation/templates/federation" / name).read_text(encoding="utf-8")
        assert '<div class="empty-state">' in template and 'class="card empty-state"' not in template


def test_federation_pairing_key_wraps_in_a_monospace_field():
    template = feature_file("federation", "templates", "federation", "admin.html")
    assert '<textarea class="textarea mono textarea--key" id="f-secret" name="secret"' in template
    assert 'class="empty-state"' in template
