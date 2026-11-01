"""Regression tests for the r2 findings of the full re-audit pass 12 (bulk Markdown import memory,
the check-out directory cost and pending proposals read on page views)."""

from __future__ import annotations

import io
import re
import zipfile

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.page_export import bulk
from bananawiki.wiki.features.pages import service as pages
from tests.features.pages_support import make_category, make_page, restrict

# RW2-3: the unpacked bound of the bulk Markdown import covers the whole request, not each archive


def _archive(prefix: str, count: int, size: int) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for n in range(count):
            archive.writestr(f"{prefix}-{n}.md", f"# {prefix} {n}\n\n" + "a" * (size - 20))
    buffer.seek(0)
    return buffer


def test_rw2_3_bulk_import_bounds_the_unpacked_total_of_the_request(app, admin_client, monkeypatch):
    monkeypatch.setattr(bulk, "MAX_TOTAL_BYTES", 10_000)
    held: list[int] = []
    real = bulk.import_files
    monkeypatch.setattr(bulk, "import_files", lambda files, **kw: (held.append(sum(len(t) for _, t in files)),
                                                                  real(files, **kw)))
    # Each archive (4 x 1,000 bytes) fits the bound on its own; together they would hold 12,000.
    data = {"import_file": [(_archive(name, 4, 1_000), f"{name}.zip") for name in ("one", "two", "three")]
            + [(io.BytesIO(b"# Loose\n\n" + b"b" * 2_500), "loose.md"), (io.BytesIO(b"# Small\n"), "small.md")]}
    response = admin_client.post("/admin/bulk-markdown", data=data, content_type="multipart/form-data",
                                 follow_redirects=True)
    assert response.status_code == 200
    assert held and held[0] <= 10_000
    text = response.get_data(as_text=True)
    assert "three.zip is too large once unpacked." in text and "loose.md is too large once unpacked." in text
    with app.app_context():
        assert pages.get_by_slug("one-0") is not None and pages.get_by_slug("two-3") is not None
        assert pages.get_by_slug("three-0") is None and pages.get_by_slug("loose") is None
        assert pages.get_by_slug("small") is not None


# RW2-4: the check-out directory filters and windows in SQL, and has its own rate limit


def _turn_on_reservations(app) -> None:
    from bananawiki.wiki import registry
    from bananawiki.wiki.db import connection_scope
    from tests.features.governance_support import set_settings

    with app.test_request_context(), connection_scope():
        registry.set_enabled("page_governance", True)
    set_settings(app, page_protection_enabled=1, page_reservations_enabled=1)


def _directory_slugs(client, number: int) -> list[str]:
    response = client.get(f"/reservations?page={number}")
    assert response.status_code == 200
    table = response.get_data(as_text=True).split("governance-directory", 1)
    return re.findall(r'<td><a href="/page/([^"]+)">', table[1].split("</table>", 1)[0]) if len(table) > 1 else []


def test_rw2_4_directory_lists_editable_pages_one_window_at_a_time(app, client, make_user, login, db,
                                                                   monkeypatch):
    from bananawiki.wiki.features.page_governance import routes as governance
    from tests.features.governance_support import as_user

    _turn_on_reservations(app)
    writable, readable = make_category(app, "Writable")["id"], make_category(app, "Readable")["id"]
    for n in range(5):
        make_page(app, f"Page {n}", category_id=writable)
    make_page(app, "Read only", category_id=readable)
    make_page(app, "Loose")
    gone = make_page(app, "Going away", category_id=writable)
    db.execute("UPDATE pages SET pending_deletion = 1 WHERE id = ?", (gone["id"],))
    scoped, viewer, boss = (make_user("scoped", role="editor"), make_user("viewer", role="editor"),
                            make_user("boss", role="admin"))
    restrict(db, scoped, read=[writable, readable], write=[writable])
    restrict(db, viewer, keys={"page.view_all"})
    monkeypatch.setattr(governance, "DIRECTORY_PAGE_SIZE", 2)
    real = pages.can_edit
    calls: list[int] = []
    monkeypatch.setattr(pages, "can_edit", lambda page, user=None: (calls.append(1), real(page, user))[1])

    def expected(user):
        with app.test_request_context(), connection_scope():
            as_user(user)
            rows = db.all("SELECT * FROM pages ORDER BY title COLLATE NOCASE, id")
            return [p["slug"] for p in rows if not p["is_home"] and not p["pending_deletion"] and real(p, user)]

    for user, count in ((scoped, 5), (viewer, 0), (boss, 7)):
        want = expected(user)
        assert len(want) == count
        login(client, user)
        calls.clear()
        listed = [slug for number in range(1, 6) for slug in _directory_slugs(client, number)[:2]]
        # Pages past the last one repeat the last window.
        assert listed[:count] == want and set(listed) == set(want)
        assert calls == []  # no per-page edit check: the rights are applied in SQL
        client.post("/logout")


def test_rw2_4_directory_has_its_own_rate_limit(app, client, make_user, login, frozen_limiter_clock):
    _turn_on_reservations(app)
    login(client, make_user("busy", role="editor"))
    assert all(client.get("/reservations").status_code == 200 for _ in range(30))
    assert client.get("/reservations").status_code == 429


# RW3-1: the reviewer's badge and the page notice never read the texts of pending proposals


def test_rw3_1_page_views_of_reviewers_never_load_proposed_texts(app, client, make_user, login, db, monkeypatch):
    from bananawiki.core.sqlite import Session
    from bananawiki.wiki import permissions, registry
    from bananawiki.wiki.features.contributions import service as contributions
    from tests.features.governance_support import as_user

    with app.test_request_context(), connection_scope():
        registry.set_enabled("contributions", True)
    targets = [make_page(app, f"Target {n}", "old text") for n in range(2)]
    marker = "PROPOSED-TEXT-MARKER"
    for n in range(3):
        proposer = make_user(f"proposer{n}")
        with app.test_request_context(), connection_scope():
            as_user(proposer)
            for page in targets:
                contributions.propose(pages.get(page["id"]), proposer, title=None, content=marker * 50,
                                      reason=f"reason {n}")
    reviewer, boss = make_user("rev_ed", role="editor"), make_user("boss", role="admin")
    restrict(db, reviewer, keys=set(permissions.defaults("editor")) | {"contribution.review"})
    with app.test_request_context(), connection_scope():
        assert contributions.review_count(reviewer) == 6
        assert contributions.oldest_for_reviewer(reviewer) is not None
        assert [row["reason"] for row in contributions.pending_for_reviewer(reviewer)][:1] == ["reason 0"]

    loaded: list[str] = []
    real_all, real_one = Session.all, Session.one

    def spy(real):
        def query(self, sql, params=()):
            result = real(self, sql, params)
            for row in result if isinstance(result, list) else [result] if result else []:
                if any(isinstance(value, str) and marker in value for value in row.values()):
                    loaded.append(sql)
            return result
        return query

    monkeypatch.setattr(Session, "all", spy(real_all))
    monkeypatch.setattr(Session, "one", spy(real_one))
    for user in (reviewer, boss):
        login(client, user)
        for url in ("/settings", f"/page/{targets[0]['slug']}", "/admin/contributions"):
            response = client.get(url)
            assert response.status_code == 200, url
        assert loaded == [], loaded
    # The notice still counts the proposals waiting on the page.
    assert "3 proposed edits are waiting for review" in client.get(f"/page/{targets[0]['slug']}").get_data(as_text=True)
