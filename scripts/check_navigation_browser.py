#!/usr/bin/env python3
"""Check sidebar paging and editing in Chromium with disposable local data."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def serve_fixture(directory):
    for key in list(os.environ):
        if key.startswith(("BW_", "HOSTING_", "BANANA_")):
            del os.environ[key]
    os.environ.update(BW_ENV="testing", BW_INSTANCE_DIR=str(directory),
                      BW_DATABASE_PATH=str(directory / "wiki.db"),
                      BW_LOGGING_LEVEL="off", BW_SECURE_COOKIES="0", BW_PROXY_MODE="0")
    sys.path.insert(0, str(ROOT))
    import db
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    db.init_db()
    username, password = "browseradmin", "Disposable-browser-fixture-42"
    user = db.create_user(username, generate_password_hash(password), role="admin")
    db.update_site_settings(setup_done=1)
    category = db.create_category("Browser <category>")
    with db.get_db_context() as connection:
        connection.executemany(
            "INSERT INTO pages(title,slug,content,last_edited_by,sort_order,category_id) VALUES(?,?,?,?,?,?)",
            [(f"Page {i:03}", f"browser-{i}", "Browser regression content.", user, i, category)
             for i in range(251)],
        )
        connection.commit()
        ids = [row[0] for row in connection.execute("SELECT id FROM pages WHERE is_home=0 ORDER BY sort_order,title,id")]
    from app import app
    # Skip only the anti-bot timing challenge; retain real sessions and CSRF.
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    state = directory / ".ready.json"
    state.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                                 "username": username, "password": password,
                                 "category": category, "ids": ids}))
    state.chmod(0o600)
    state.replace(directory / "ready.json")
    server.serve_forever()


def check_navigation(state, output):
    from playwright.sync_api import expect, sync_playwright

    results, expected = [], state["ids"][:]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for label, width, height in (("desktop", 1280, 900), ("mobile", 390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error, sink=errors: sink.append(str(error)))
                page.set_default_timeout(10000)
                try:
                    private = context.request.get(state["url"] + "/api/sidebar/pages", max_redirects=0)
                    assert private.status in (302, 401), private.status
                    page.goto(state["url"] + "/login", wait_until="networkidle")
                    page.locator('input[name="username"]').fill(state["username"])
                    page.locator('input[name="password"]').fill(state["password"])
                    page.locator('button[type="submit"]').click()
                    page.wait_for_load_state("networkidle")
                    page.goto(state["url"] + "/page/browser-250", wait_until="networkidle")
                    assert page.url.endswith("/page/browser-250"), "Login did not establish a session"
                    if label == "mobile":
                        page.locator("#sidebar-toggle").click()
                    category = page.locator(f'.nav-section[data-cat-id="{state["category"]}"]')
                    if "collapsed" in (category.get_attribute("class") or "").split():
                        category.locator(".cat-toggle").first.click()
                    rows = category.locator(".nav-item-row")
                    expect(rows).to_have_count(100)
                    expect(page.locator(".sidebar-current-page")).to_have_count(1)
                    assert page.locator(".cat-modal-src").count() == 0

                    # A failed batch remains available for retry.
                    page.route("**/api/sidebar/pages?*", lambda route: route.fulfill(
                        status=503, content_type="application/json", body='{"error":"Fixture unavailable"}'))
                    more = category.locator("[data-sidebar-load]")
                    more.click()
                    expect(more).not_to_have_attribute("aria-busy", "true")
                    expect(rows).to_have_count(100)
                    if label == "mobile":
                        expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
                    page.unroute("**/api/sidebar/pages?*")

                    # Failure restores the visible order and leaves the cursor alone.
                    def row_ids(rows=rows):
                        return [int(value) for value in rows.evaluate_all("items => items.map(item => item.dataset.pageId)")]
                    initial = row_ids()
                    assert initial == expected[:100]
                    page.route("**/api/reorder/pages", lambda route: route.fulfill(
                        status=503, content_type="application/json", body='{"error":"Fixture unavailable"}'))
                    rows.last.hover()
                    rows.last.locator('[data-dir="up"]').click()
                    page.locator("#bw-confirm-ok").click()
                    expect(category.locator("[data-reorder-pending]")).to_have_count(0)
                    assert row_ids() == initial
                    page.unroute("**/api/reorder/pages")

                    # Reorder at the loaded boundary, then load every remaining row.
                    rows.last.hover()
                    expect(rows.last.locator('[data-dir="up"]')).not_to_have_attribute("data-bw-click-locked", "1")
                    rows.last.locator('[data-dir="up"]').click()
                    expect(page.locator("#bw-confirm-ok")).not_to_have_attribute("data-bw-click-locked", "1")
                    page.locator("#bw-confirm-ok").click()
                    expect(category.locator("[data-reorder-pending]")).to_have_count(0)
                    expected[98], expected[99] = expected[99], expected[98]
                    assert row_ids() == expected[:100]
                    while category.locator("[data-sidebar-load]").count():
                        count = rows.count()
                        category.locator("[data-sidebar-load]").click()
                        expect(rows).to_have_count(min(count + 50, 251))
                    assert row_ids() == expected
                    expect(page.locator(".sidebar-current-page")).to_have_count(0)
                    expect(rows.locator("a.active")).to_have_count(1)

                    # Category management is fetched only when opened and keeps CSRF.
                    category.locator(".nav-section-header").first.hover()
                    category.locator("[data-open-cat-modal]").click()
                    modal = page.locator(f'#catManageModal{state["category"]}')
                    expect(modal).to_be_visible()
                    expect(modal.locator("[data-cat-page-count]")).to_have_attribute("data-cat-page-count", "251")
                    form = modal.locator('form').first
                    assert form.locator('input[name="csrf_token"]').input_value()
                    form.locator('input[name="name"]').fill("Browser category " + label)
                    form.locator('button[type="submit"]').click()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator(f'.nav-section[data-cat-id="{state["category"]}"] .nav-section-title').first).to_have_text("Browser category " + label)
                    assert not errors, errors
                    page.screenshot(path=str(output / f"{label}-navigation.png"), full_page=True)
                    menu = page.locator('details[data-action-menu]')
                    summary = menu.locator('summary')
                    summary.focus()
                    page.keyboard.press('Enter')
                    expect(menu).to_have_attribute('open', '')
                    menu.locator('[data-modal="titleModal"]').click()
                    expect(page.locator('#titleModal')).to_be_visible()
                    page.locator('#titleModal .js-close-modal').click()
                    expect(page.locator('#titleModal')).not_to_be_visible()
                    summary.click()
                    page.keyboard.press('Escape')
                    expect(menu).not_to_have_attribute('open', '')
                    expect(summary).to_be_focused()
                    results.append({"viewport": label, "paging_complete": True, "retry_after_failure": True,
                                    "reorder_failure_rolled_back": True, "reordered_cursor_complete": True,
                                    "active_link_preserved": True, "category_edit_with_csrf": True,
                                    "page_actions_keyboard_and_modal": True,
                                    "javascript_errors": errors})
                except BaseException:
                    page.screenshot(path=str(output / f"{label}-failure.png"), full_page=True)
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".browser-artifacts"))
    parser.add_argument("--server-python", default=sys.executable)
    args = parser.parse_args()
    if args.serve_fixture:
        serve_fixture(args.serve_fixture)
        return 0
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="banana-navigation-browser-") as temporary:
        directory = Path(temporary)
        with (output / "server.log").open("w") as log:
            child = subprocess.Popen([args.server_python, str(Path(__file__).resolve()), "--serve-fixture", str(directory)],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            try:
                ready = directory / "ready.json"
                deadline = time.monotonic() + 45
                while not ready.exists():
                    if child.poll() is not None:
                        raise RuntimeError("Browser fixture failed; see server.log")
                    if time.monotonic() > deadline:
                        raise TimeoutError("Browser fixture did not start")
                    time.sleep(0.1)
                check_navigation(json.loads(ready.read_text()), output)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    print("Desktop and mobile navigation checks passed; temporary server and data removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
