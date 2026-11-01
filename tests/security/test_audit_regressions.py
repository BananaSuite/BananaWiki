"""Regression tests for the findings of the security audit (AUDIT.md), one or more per finding.

Each test names its finding (``W3-1`` ...) and reproduces the attack: it
failed before the fix and must keep passing.
"""

from __future__ import annotations

import io

from tests.conftest import PASSWORD
from tests.features.plugins_support import row, set_row, start_enabled, write_plugin

PLUGIN = """
from flask import Blueprint
from bananawiki.wiki.registry import Feature
from bananawiki.wiki import auth
bp = Blueprint('probe_plugin', __name__)
@bp.get('/probe-plugin/ping')
@auth.public
def ping():
    return 'plugin-code-active'
FEATURE = Feature(id='probe_plugin', name='Probe', blueprints=[bp])
"""


def test_w3_1_plain_admin_cannot_switch_a_plugin_on_through_the_onboarding_wizard(
        app_factory, tmp_path, make_user, login):
    write_plugin(tmp_path, "probe_plugin", PLUGIN)
    app = start_enabled(app_factory, tmp_path, "probe_plugin")
    set_row(app, "probe_plugin", False)
    client = app.test_client()
    login(client, make_user("plainadmin", role="admin"))

    assert b"probe_plugin" not in client.get("/onboarding").data
    client.post("/onboarding", data={"site_name": "W", "mode": "advanced", "features": ["probe_plugin"],
                                     "default_theme_mode": "dark"})

    assert row(app, "probe_plugin")["enabled"] == 0
    assert client.get("/probe-plugin/ping").status_code == 404


def test_w3_1_owner_still_switches_a_plugin_on_through_the_wizard(app_factory, tmp_path, make_user, login):
    write_plugin(tmp_path, "probe_plugin", PLUGIN)
    app = start_enabled(app_factory, tmp_path, "probe_plugin")
    set_row(app, "probe_plugin", False)
    client = app.test_client()
    login(client, make_user("theowner", role="owner"))

    client.post("/onboarding", data={"site_name": "W", "mode": "advanced", "features": ["probe_plugin"],
                                     "default_theme_mode": "dark", "password": PASSWORD})

    assert row(app, "probe_plugin")["enabled"] == 1


# W3-2 / W1-2: request bodies and the global limit before CSRF

MIB = 1024 * 1024


def _small_body_wiki(app_factory):
    app = app_factory(environ={"BW_MAX_CONTENT_LENGTH_BYTES": str(MIB)})
    app.config["CSRF_DISABLED"] = False
    return app


def _csrf(client) -> str:
    from tests.conftest import csrf_token_from

    return csrf_token_from(client.get("/login"))


def test_w3_2_anonymous_bodies_are_capped_before_csrf_reads_them(app_factory):
    client = _small_body_wiki(app_factory).test_client()
    token = _csrf(client)
    big = {"csrf_token": token, "username": "x", "file": (io.BytesIO(b"x" * (2 * MIB)), "big.bin")}
    assert client.post("/login", data=big, content_type="multipart/form-data").status_code == 413
    upload = {"csrf_token": token, "file": (io.BytesIO(b"x" * (2 * MIB)), "big.pdf")}
    assert client.post("/page/home/attachments", data=upload,
                       content_type="multipart/form-data").status_code == 413


def test_w3_2_signed_in_editor_still_uploads_large_attachments(app_factory, make_user, login):
    from tests.features.pages_support import make_page

    app = _small_body_wiki(app_factory)
    page = make_page(app, "Guide", "text")
    client = app.test_client()
    editor = make_user("att_big", role="editor")
    token = _csrf(client)
    client.post("/login", data={"csrf_token": token, "username": editor["username"], "password": PASSWORD})
    token = _token_after_login(client, page)
    response = client.post(f"/page/{page['slug']}/attachments", content_type="multipart/form-data",
                           data={"csrf_token": token, "file": (io.BytesIO(b"%PDF-1.4" + b"x" * (2 * MIB)), "big.pdf")})
    assert response.status_code == 302


def _token_after_login(client, page) -> str:
    from tests.conftest import csrf_token_from

    return csrf_token_from(client.get(f"/page/{page['slug']}"))


def test_w1_2_requests_refused_by_csrf_count_against_the_global_limit(app_factory, frozen_limiter_clock):
    from bananawiki.wiki.app import GLOBAL_RATE_LIMIT

    client = _small_body_wiki(app_factory).test_client()
    statuses = {client.post("/login", data={"username": "x"}).status_code for _ in range(GLOBAL_RATE_LIMIT + 1)}
    assert 429 in statuses


def test_r1_1_a_bogus_bearer_header_does_not_unlock_large_bodies(app_factory):
    from bananawiki.wiki.db import connection_scope
    from tests.features.api_support import enable_api

    app = _small_body_wiki(app_factory)
    with app.app_context(), connection_scope() as session:
        enable_api(app, session)
    client = app.test_client()
    token = _csrf(client)
    junk = {"Authorization": "Bearer not-a-token"}
    upload = {"csrf_token": token, "file": (io.BytesIO(b"x" * (2 * MIB)), "big.pdf")}
    assert client.post("/page/home/attachments", data=upload, headers=junk,
                       content_type="multipart/form-data").status_code == 413
    upload = {"file": (io.BytesIO(b"x" * (2 * MIB)), "big.pdf")}
    # The API turns the token away before it reads the body.
    assert client.post("/api/v1/pages/home/attachments", data=upload, headers=junk,
                       content_type="multipart/form-data").status_code in (401, 413)


def test_r1_1_a_valid_api_token_still_uploads_large_attachments(app_factory, make_user):
    from bananawiki.wiki.db import connection_scope
    from tests.features.api_support import enable_api, issue

    app = _small_body_wiki(app_factory)
    with app.app_context(), connection_scope() as session:
        enable_api(app, session)
        session.execute("UPDATE users SET api_access_enabled = 1")
    editor = make_user("api_big", role="editor", api_access_enabled=1)
    raw = issue(app, editor, scopes=("pages", "attachments"))
    response = app.test_client().post(
        "/api/v1/pages/home/attachments", headers={"Authorization": f"Bearer {raw}"},
        data={"file": (io.BytesIO(b"%PDF-1.4" + b"x" * (2 * MIB)), "big.pdf")}, content_type="multipart/form-data")
    assert response.status_code != 413, response.data[:300]


def _signed_in_client(app, user):
    from tests.conftest import csrf_token_from

    client = app.test_client()
    token = csrf_token_from(client.get("/login"))
    client.post("/login", data={"csrf_token": token, "username": user["username"], "password": PASSWORD})
    return client


def test_r2_1_blocked_and_plain_accounts_do_not_get_large_bodies(app_factory, make_user):
    from tests.conftest import csrf_token_from

    app = _small_body_wiki(app_factory)
    pending = _signed_in_client(app, make_user("pending9", approval_status="pending"))
    token = csrf_token_from(pending.get("/account-status"))
    upload = {"csrf_token": token, "file": (io.BytesIO(b"x" * (2 * MIB)), "big.pdf")}
    assert pending.post("/page/home/attachments", data=upload, content_type="multipart/form-data").status_code == 413

    plain = _signed_in_client(app, make_user("plain9"))
    token = csrf_token_from(plain.get("/"))
    upload = {"csrf_token": token, "file": (io.BytesIO(b"x" * (2 * MIB)), "site.zip")}
    assert plain.post("/admin/migration/import", data=upload, content_type="multipart/form-data").status_code == 413


def test_r2_2_switching_bm_mode_on_does_not_pause_a_temporary_promotion(app, make_user, login, db):
    import json
    from datetime import timedelta

    from bananawiki.core.timeutil import to_sql, utcnow

    db.execute("UPDATE site_settings SET bm_choices = ?, bm_mode = 0, bm_mode_env = 0",
               (json.dumps({"feature:temporary_accounts": False}),))
    owner = make_user("theowner", role="owner")
    temp = make_user("tempadmin", role="admin")
    db.execute("INSERT INTO plugins (id, name, version, builtin, enabled, installed_at) "
               "VALUES ('temporary_accounts', 'x', '1', 1, 1, '2026-01-01 00:00:00') "
               "ON CONFLICT(id) DO UPDATE SET enabled = 1")
    past = to_sql(utcnow() - timedelta(minutes=1))
    db.execute("INSERT INTO temp_roles (user_id, original_role, expires_at, show_countdown, set_by, created_at, "
               "updated_at) VALUES (?, 'user', ?, 0, ?, ?, ?)", (temp["id"], past, owner["id"], past, past))
    client = app.test_client()
    login(client, temp)

    client.post("/admin/bm-mode", data={"bm_mode": "1"})
    app.extensions["bananawiki.scheduler"].run_due(force=True, only="temporary_accounts.expire")

    assert db.scalar("SELECT role FROM users WHERE id = ?", (temp["id"],)) == "user"


def test_r1_2_head_requests_do_not_use_up_the_attachment_zip_slots(app, admin_client, make_user, login):
    from bananawiki.wiki.features.attachments import service
    from tests.features.pages_support import make_page

    page = make_page(app, "Files")
    admin_client.post(f"/page/{page['slug']}/attachments", data={"files": (io.BytesIO(b"x" * 1000), "a.bin")},
                      content_type="multipart/form-data")
    reader = app.test_client()
    login(reader, make_user("plainreader"))
    url = f"/page/{page['slug']}/attachments/download-all"
    for _ in range(service.MAX_ZIP_STREAMS + 1):
        reader.head(url).close()

    response = admin_client.get(url)
    assert response.status_code == 200
    response.close()
    assert service._zip_streams.acquire(blocking=False)  # every slot is back
    service._zip_streams.release()


def test_r1_3_exponential_lexers_are_stopped_at_the_highlighting_time(monkeypatch):
    """Rita and Bash need exponential time inside one token on a few dozen characters."""
    import time

    from bananawiki.wiki import markdown

    markdown.clear_render_cache()
    fence = "`" * 3
    markdown.render(fence + "python\nx = 1\n" + fence + "\n")  # start the helper first
    for language, code in (("rita", "'" + "0" * 40 + "x"), ("bash", "$'" + "\\" * 70)):
        started = time.monotonic()
        html = markdown.render(f"{fence}{language}\n{code}\n{fence}\n")
        assert time.monotonic() - started < markdown.MAX_HIGHLIGHT_SECONDS + 1.5
        assert "<code" in html  # shown as plain code
    assert '<span class="n">x</span>' in markdown.render(fence + "python\nx = 2\n" + fence + "\n")


def test_s1_1_waiting_for_a_busy_highlighter_counts_against_the_time(app, make_user):
    """While other readers keep both helpers busy, one canvas answer stays within its highlighting time."""
    import itertools
    import threading
    import time

    from bananawiki.wiki import markdown
    from bananawiki.wiki.features.canvas import present

    counter, stop = itertools.count(), threading.Event()
    slow = "x\n" * 30000  # about 0.6 s of C# lexing: it uses up its time and stays uncached

    def other_reader():
        while not stop.is_set():
            with app.test_request_context():
                present.present_nodes([{"id": "s", "type": "code", "language": "csharp",
                                        "content": slow + f"// {next(counter)}"}], None)

    readers = [threading.Thread(target=other_reader, daemon=True) for _ in range(3)]
    for reader in readers:
        reader.start()
    time.sleep(1.0)
    nodes = [{"id": f"n{n}", "type": "code", "language": "python", "content": f"x = {n}  # {time.time()}"}
             for n in range(20)]
    started = time.monotonic()
    with app.test_request_context():
        present.present_nodes(nodes, make_user("viewer"))
    took = time.monotonic() - started
    stop.set()
    for reader in readers:
        reader.join()
    assert took < markdown.MAX_HIGHLIGHT_SECONDS + 1.5


def test_t1_4_a_page_rendered_while_the_helpers_were_busy_is_not_kept_plain():
    import threading
    import time

    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name

    from bananawiki.wiki import highlighter, markdown

    markdown.clear_render_cache()
    fence = "`" * 3
    markdown.render(fence + "python\nwarm = 1\n" + fence + "\n")
    slow = "$'" + "\\" * 70 + "\n"
    hogs = [threading.Thread(target=highlighter.highlight, args=([slow], get_lexer_by_name("bash"),
                                                                 HtmlFormatter(), 3.0))
            for _ in range(highlighter.HELPERS)]
    for hog in hogs:
        hog.start()
    time.sleep(0.3)
    page = fence + "python\ndef victim():\n    return 42\n" + fence + "\n" + ("Ordinary page text. " * 120)
    markdown.render(page)
    for hog in hogs:
        hog.join()
    assert '<span class="k">def</span>' in markdown.render(page)


def test_u1_3_a_canvas_node_rendered_while_the_helpers_were_busy_is_not_kept_plain(app, monkeypatch):
    from bananawiki.wiki import highlighter, markdown
    from bananawiki.wiki.features.canvas import present

    markdown.clear_render_cache()
    present._cache.clear()
    node = {"id": "n1", "type": "code", "language": "python", "content": "def victim():\n    return 42\n"}
    real_take = highlighter._take
    monkeypatch.setattr(highlighter, "_take", lambda seconds: None)  # no helper free in time
    with app.test_request_context():
        present.rendering(node)  # what POST /canvas/<slug>/render does
    monkeypatch.setattr(highlighter, "_take", real_take)
    with app.test_request_context():
        later = present.present_nodes([node], None)["rendered"]
    assert '<span class="k">def</span>' in str(later)
