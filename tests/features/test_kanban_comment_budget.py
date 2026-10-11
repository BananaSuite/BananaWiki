"""Listing a ticket's comments costs a bounded amount of work per request (second 1.7 audit).

* The comments of one listing share one highlighting allowance, like the
  blocks of a page; what a listing could not highlight comes from later ones.
* A ticket holds at most ``MAX_COMMENTS_PER_TICKET`` comments, and a listing
  renders at most that many as Markdown (older tickets may hold more: those are
  listed as plain text).
* What a listing rendered is kept, however short, so opening the ticket again
  renders nothing.
* A listing carries a bounded amount of rendered HTML: comments whose Markdown
  expands a lot (reference links repeated a few hundred times) cannot make each
  listing render and send tens of megabytes. Past the budget, older comments
  are listed as plain text.
"""

from __future__ import annotations

import pytest
from markdown.extensions import codehilite

from bananawiki.wiki import markdown
from bananawiki.wiki.features.kanban import fields

from .test_kanban import board_id_from, columns, new_ticket

LINE = "a+b-c*d/e%f&g|h^i<j>k!l~m?n:o;p,q.r(s)t[u]v{w}x=y\n"


@pytest.fixture(autouse=True)
def _fresh_cache():
    markdown.clear_render_cache()
    yield
    markdown.clear_render_cache()


@pytest.fixture
def ticket(admin_client):
    board = board_id_from(admin_client.post("/kanban/create", data={"title": "Roadmap"}))
    return new_ticket(admin_client, columns(admin_client, board)[0]["id"])


@pytest.fixture
def highlighted(monkeypatch):
    """Characters of code handed to Pygments."""
    sizes: list[int] = []
    original = codehilite.highlight

    def counting(source, lexer, formatter):
        sizes.append(len(source))
        return original(source, lexer, formatter)

    monkeypatch.setattr(codehilite, "highlight", counting)
    return sizes


def _comment_rows(db, ticket_id, user_id, contents):
    for content in contents:
        db.execute("INSERT INTO kanban_ticket_comments (ticket_id, user_id, content, created_at, updated_at) "
                   "VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')", (ticket_id, user_id, content))


def _list(client, ticket):
    response = client.get(f"/api/kanban/tickets/{ticket['id']}/comments")
    assert response.status_code == 200
    return response.get_json()


def test_the_comments_of_a_listing_share_one_highlighting_allowance(admin_client, db, ticket, admin, highlighted):
    code = ["```c\n" + f"/* {n} */\n" + LINE * 30 + "```" for n in range(60)]  # ~1.5 KB of code each
    _comment_rows(db, ticket["id"], admin["id"], code)
    listed = _list(admin_client, ticket)
    assert len(listed) == 60 and all(f"/* {n} */" in c["content_html"] for n, c in enumerate(listed))
    spent = sum(highlighted) + markdown.HIGHLIGHT_BLOCK_COST * len(highlighted)
    assert highlighted and spent <= markdown.MAX_HIGHLIGHT_DOCUMENT_CHARS
    # What was highlighted is kept; the next listing highlights more of the rest.
    first = len(highlighted)
    _list(admin_client, ticket)
    assert first < len(highlighted) <= 2 * first


def test_a_listing_keeps_its_renderings_so_the_next_one_renders_nothing(admin_client, db, ticket, admin,
                                                                        monkeypatch):
    # Short comments render faster than most cache entries are worth, but a listing renders
    # up to MAX_COMMENTS_PER_TICKET of them at once, every time the ticket is opened.
    _comment_rows(db, ticket["id"], admin["id"], [f"- item **{n}**\n  - nested `{n}`" for n in range(40)])
    rendered = []
    original = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args: rendered.append(1) or original(*args))
    first = _list(admin_client, ticket)
    assert len(rendered) == 40
    assert _list(admin_client, ticket) == first and len(rendered) == 40


def test_a_ticket_holds_a_bounded_number_of_comments(admin_client, db, ticket, admin):
    _comment_rows(db, ticket["id"], admin["id"], [f"note {n}" for n in range(fields.MAX_COMMENTS_PER_TICKET - 1)])
    url = f"/api/kanban/tickets/{ticket['id']}/comments"
    assert admin_client.post(url, json={"content": "the last one"}).status_code == 201
    refused = admin_client.post(url, json={"content": "one too many"})
    assert refused.status_code == 409
    assert refused.get_json()["error"] == (
        f"This ticket has reached the limit of {fields.MAX_COMMENTS_PER_TICKET} comments.")
    assert db.scalar("SELECT COUNT(*) FROM kanban_ticket_comments WHERE ticket_id = ?",
                     (ticket["id"],)) == fields.MAX_COMMENTS_PER_TICKET


def test_older_tickets_past_the_limit_list_their_oldest_comments_as_plain_text(admin_client, db, ticket, admin,
                                                                               monkeypatch):
    extra = 3
    _comment_rows(db, ticket["id"], admin["id"],
                  [f"**note** {n}" for n in range(fields.MAX_COMMENTS_PER_TICKET + extra)])
    rendered = []
    original = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args: rendered.append(1) or original(*args))
    listed = _list(admin_client, ticket)
    assert len(listed) == fields.MAX_COMMENTS_PER_TICKET + extra
    assert len(rendered) == fields.MAX_COMMENTS_PER_TICKET
    assert [c["content_html"] for c in listed[:extra]] == [f"<pre>**note** {n}</pre>" for n in range(extra)]
    assert listed[extra]["content_html"] == f"<p><strong>note</strong> {extra}</p>"
    assert listed[-1]["content"] == f"**note** {fields.MAX_COMMENTS_PER_TICKET + extra - 1}"


def _expanding_comment(n: int) -> str:
    """A comment of at most MAX_COMMENT characters whose HTML is about a hundred times longer."""
    definition = f"[a]: http://example.com/{'x' * 960}{n:04d}\n\n"
    return definition + "[a][]" * ((fields.MAX_COMMENT - len(definition)) // 5)


def test_a_listing_renders_a_bounded_amount_of_html(admin_client, db, ticket, admin, monkeypatch):
    # Second 1.7 audit (WCA-1 follow-up): the highlighting allowance bounded code, but
    # reference links expand each comment to ~200 KB, so 300 of them made every listing
    # render and send ~60 MB.
    url = f"/api/kanban/tickets/{ticket['id']}/comments"
    count = 60
    _comment_rows(db, ticket["id"], admin["id"], [_expanding_comment(n) for n in range(count - 1)])
    newest = _expanding_comment(count)
    assert admin_client.post(url, json={"content": newest}).status_code == 201
    rendered = []
    original = markdown._render
    monkeypatch.setattr(markdown, "_render", lambda *args: rendered.append(1) or original(*args))
    response = admin_client.get(url)
    assert response.status_code == 200
    listed = response.get_json()
    assert len(listed) == count
    # The budget, plus the one comment that crossed it, plus the rest as typed.
    assert sum(len(c["content_html"]) for c in listed) < fields.MAX_LISTING_HTML_CHARS + 500_000
    assert len(response.data) < 2 * fields.MAX_LISTING_HTML_CHARS
    assert len(rendered) < count // 2
    # The newest comments are the ones rendered; older ones are listed as typed.
    assert listed[-1]["content"] == newest and listed[-1]["content_html"].startswith("<p><a href=")
    assert listed[0]["content_html"].startswith("<pre>[a]: http://example.com/")
    # Later listings send the same and render nothing new.
    assert admin_client.get(url).get_json() == listed and len(rendered) < count // 2
