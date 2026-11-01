"""Interface formatting helpers: plural choice, escaping, sizes, digits and device labels."""

from __future__ import annotations

from bananawiki.wiki import i18n, templating


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
