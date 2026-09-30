from bananawiki.wiki.markdown import render, video_embed_src


def test_scripts_and_handlers_are_removed():
    html = render('<script>alert(1)</script><img src="x" onerror="alert(1)"><a href="javascript:alert(1)">x</a>')
    assert "<script" not in html and "onerror" not in html and "javascript:" not in html


def test_classes_and_ids_are_restricted():
    html = render('<div class="navbar" id="csrf_token">x</div>\n\n# Heading')
    assert "navbar" not in html and 'id="csrf_token"' not in html
    assert '<h1 id="heading">' in html


def test_links_get_noopener():
    html = render('<a href="https://example.com" target="_blank">x</a>')
    assert 'rel="noopener noreferrer"' in html and 'target="_blank"' in html


def test_fenced_code_keeps_blank_lines():
    html = render("```\na\n\n\n\nb\n```")
    assert "<br>" not in html


def test_blank_line_runs_outside_code_are_kept():
    assert "<br><br>" in render("a\n\n\n\n\nb")


def test_lists_after_paragraph_and_two_space_nesting():
    html = render("Intro:\n1. a\n2. b\n  - nested")
    assert "<ol>" in html and "<ul>" in html


def test_video_shortcode_and_bare_link():
    html = render('text\n[[video url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" align="left"]]\nmore')
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in html and "bw-video-left" in html
    assert "player.vimeo.com/video/123" in render("https://vimeo.com/123")


def test_video_src_rejects_other_hosts():
    assert video_embed_src("https://evil.example/watch?v=dQw4w9WgXcQ") is None


def test_embed_shortcodes():
    html = render('[[kanban board="Team board" height="300"]]')
    assert 'data-embed-type="kanban"' in html and 'data-embed-ref="Team board"' in html


def test_mentions_are_linked_outside_code():
    html = render("hi @alice and `@bob`")
    assert 'href="/users/alice"' in html and "/users/bob" not in html


def test_strikethrough_and_task_lists():
    html = render("~~gone~~ kept\n\n- [ ] todo\n- [x] done")
    assert "<del>gone</del>" in html
    assert 'class="task-item task-open"' in html and 'class="task-item task-done"' in html
    assert "[ ]" not in html
    assert "~~" in render("`~~code~~`")


def test_task_marker_in_code_is_untouched():
    assert "[ ]" in render("```\n- [ ] x\n```")
