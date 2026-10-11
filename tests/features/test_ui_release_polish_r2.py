"""Regression tests for the third pass of the 1.7 interface review.

Each test pins one fix from the re-check: markup a page renders, a rule the stylesheets keep,
or a string the catalogues hold in English and Italian.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki import templating
from bananawiki.wiki.markdown import render

from .chat_support import JSON, send, start_dm, switch
from .pages_support import make_category, make_page

ROOT = Path(__file__).resolve().parents[2] / "bananawiki"
CORE_CSS = (ROOT / "core/static/css/bananawiki.css").read_text(encoding="utf-8")
CONTENT_CSS = (ROOT / "wiki/static/css/content.css").read_text(encoding="utf-8")
WIKI = ROOT / "wiki"


def catalogue(lang: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(WIKI.glob(f"**/translations/{lang}.json")):
        if "examples" in path.parts:
            continue
        merged.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "_meta"})
    return merged


EN, IT = catalogue("en"), catalogue("it")


def feature_file(*parts: str) -> str:
    return (WIKI / "features" / Path(*parts)).read_text(encoding="utf-8")


# ── Catalogues ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [
    "common.breadcrumbs", "audit.detail.base_role", "audit.value.all", "attention.source.chat.reports",
    "chat.admin.ip", "chat.blocks.preview_hidden", "page_history.first_version", "custom_pages.view.copy",
    "contributions.usage_unlimited", "contributions.others_waiting_one", "contributions.quota.request_number",
    "js.kanban.due_overdue_on", "js.kanban.due_soon_on", "js.kanban.column_name", "js.kanban.list.moved",
    "kanban.mine.due_overdue", "site_admin.log.empty_errors", "site_admin.server.intro", "page_builder.settings.intro",
    "plugin_manager.order.hidden", "auth.two_step.settings_help_on", "auth.two_step.settings_help_off",
    "canvas.history.preview_hint", "users.members.publish_own", "profiles.edit_about", "admin.users.new_role",
])
def test_new_strings_exist_in_both_languages(key):
    assert EN.get(key) and IT.get(key) and IT[key] != key


def test_the_two_step_summaries_do_not_repeat_the_state():
    assert not EN["auth.two_step.admin_on"].startswith("On ") and not IT["auth.two_step.admin_on"].startswith("Attiva ")
    assert "auth.two_step.settings_help" not in EN


def test_italian_wording_fixes():
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


def test_english_permission_hints_describe_the_account_not_the_reader():
    hints = [v for k, v in EN.items() if k.startswith("permission.") and k.endswith(".hint")]
    assert not [text for text in hints if re.search(r"\byou(r)?\b", text)]


def test_numbers_and_units_do_not_break_apart():
    assert "{limit_mb} MB" in EN["site_admin.migration.file_hint"]
    assert "{limit_mb} MB" in IT["site_admin.migration.file_hint"]


# ── Stylesheets ──────────────────────────────────────────────────────────────


def test_task_boxes_sit_on_the_content_edge():
    assert re.search(r"\.prose \.task-box \{[^}]*text-indent: 0", CONTENT_CSS)
    html = render("- [x] done\n- [ ] open\n")
    assert '<span class="task-box">☑</span>done' in html and 'aria-hidden' not in html


def test_prose_cells_keep_a_date_on_one_line_and_code_blocks_show_scroll_shadows():
    assert re.search(r"\.prose th, \.prose td \{[^}]*box-sizing: content-box", CONTENT_CSS)
    assert '<td><span class="nowrap">2026-09-01</span></td>' in render("| Start |\n| --- |\n| 2026-09-01 |\n")
    assert "background:" in CONTENT_CSS.split(".prose pre {", 1)[1].split("}", 1)[0]


def test_shared_rules_of_the_third_pass():
    assert "html:has(.form-actions--sticky) { scroll-padding-bottom" in CORE_CSS
    assert ".alert__body > :last-child { margin-bottom: 0; }" in CORE_CSS
    assert re.search(r"\.stat \{[^}]*align-content: start", CORE_CSS)
    assert ":is(input, textarea, select).mono { font-family: var(--font-mono); }" in CORE_CSS
    assert "grid-template-rows: subgrid" in CORE_CSS
    assert re.search(r"\.menu__panel a, \.menu__panel button \{[^}]*justify-content: flex-start", CORE_CSS)
    # Tabs wrap on phones instead of scrolling past the edge with no cue.
    assert "flex-wrap: nowrap; overflow-x: auto" not in CORE_CSS
    assert "[data-scroll-cue]:focus-within" in CORE_CSS


# ── Filters ──────────────────────────────────────────────────────────────────


def test_capfirst_digits_and_path_breaks(app):
    assert templating.capfirst("archived “x”") == "Archived “x”"
    assert str(templating.break_after_slashes("/api/v1/pages/{slug}<x>")) == (
        "/<wbr>api/<wbr>v1/<wbr>pages/<wbr>{slug}&lt;x&gt;")
    with app.test_request_context("/?lang=it"):
        assert templating.group_digits(2457) == "2.457"
    with app.test_request_context("/"):
        assert templating.group_digits(2457) == "2,457"


# ── Pages ────────────────────────────────────────────────────────────────────


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


def test_api_guide_paths_break_after_slashes(app, db, client):
    from .api_support import enable_api

    enable_api(app, db)
    html = client.get("/api-docs").get_data(as_text=True)
    assert '<td class="api-endpoints__path"><code>/<wbr>api/<wbr>v1/<wbr>' in html


def test_category_page_lists_pages_before_subcategories(app, admin_client):
    parent = make_category(app, "Handbook")
    make_category(app, "Engineering", parent["id"])
    make_page(app, "Glossary", "x", category_id=parent["id"])
    html = admin_client.get(f"/category/{parent['id']}").get_data(as_text=True)
    body = html.split('class="page-header"', 1)[1]
    assert body.index("Glossary") < body.index("Engineering")


def test_new_category_parent_select_shows_full_paths(app, admin_client):
    parent = make_category(app, "Handbook")
    make_category(app, "Engineering", parent["id"])
    html = admin_client.get("/navigation").get_data(as_text=True)
    assert ">Handbook / Engineering</option>" in html


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


def test_contribution_edit_page_says_it_edits_a_waiting_proposal(app_factory, make_user, login):
    from bananawiki.wiki.db import connection_scope
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


def test_open_chat_reports_count_in_the_attention_total(app, client, login, make_user, admin, db):
    from bananawiki.wiki import attention
    from bananawiki.wiki.db import connection_scope

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


def test_leaderboard_link_is_not_offered_to_signed_out_visitors():
    source = (WIKI / "features/leaderboard/__init__.py").read_text(encoding="utf-8")
    assert "visible=lambda user: bool(user)" in source


def test_most_specific_admin_link_is_the_current_one(admin_client):
    html = admin_client.get("/admin/users/merge").get_data(as_text=True)
    menu = html.split('class="admin-nav"', 1)[1].split("</nav>", 1)[0]
    assert menu.count('aria-current="page"') == 1


def test_federation_pairing_key_wraps_in_a_monospace_field():
    template = feature_file("federation", "templates", "federation", "admin.html")
    assert '<textarea class="textarea mono textarea--key" id="f-secret" name="secret"' in template
    assert 'class="empty-state"' in template


def test_audit_details_are_translated():
    template = feature_file("audit", "templates", "audit", "index.html")
    assert "t('audit.detail.' ~ key, key)" in template and "t('feature.' ~ row.target_id ~ '.name'" in template
