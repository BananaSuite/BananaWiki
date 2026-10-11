"""Regressions of the third look at the portal interface (1.7.0): invalid fields that show it, an analytics
table without empty days, phone tables that keep their last column on screen, and help that names what
the screens show."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from bananawiki.core.timeutil import utcnow
from bananawiki.hosting.i18n import LANGUAGES

from .hosting_support import BOOTSTRAP, build_portal, portal_environ

PACKAGE = Path(__file__).resolve().parents[2] / "bananawiki"
HOSTING = PACKAGE / "hosting"


# ── Invalid fields ────────────────────────────────────────────────────────────


def test_an_invalid_text_field_gets_the_danger_border(tmp_path):
    css = (PACKAGE / "core" / "static" / "css" / "bananawiki.css").read_text(encoding="utf-8")
    base = css.index('input[type="text"], input[type="password"], input[type="email"]')
    invalid = css.index(':is(input, select, textarea)[aria-invalid="true"] { border-color: var(--danger); }')
    focus = css.index("input:focus, select:focus, textarea:focus {")
    # As specific as the typed-input rule and after it, so it wins; the focus ring still wins over it.
    assert base < invalid < focus
    app = build_portal(tmp_path, environ=portal_environ(tmp_path))
    form = {"username": "newperson", "password": "abcdefg1", "confirm_password": "abcdefg2", "accept_terms": "1",
            "bootstrap_token": BOOTSTRAP}
    page = app.test_client().post(f"/signup?bootstrap_token={BOOTSTRAP}", data=form).get_data(as_text=True)
    assert 'aria-invalid="true"' in re.search(r'<input id="f-confirm_password"[^>]*>', page).group(0)


# ── Analytics ─────────────────────────────────────────────────────────────────


def _summary(daily: list[dict]) -> dict:
    kinds = ("request", "page_view", "error")
    return {"window_days": len(daily), "totals": {kind: sum(row[kind] for row in daily) for kind in kinds},
            "daily": daily}


def test_the_analytics_table_lists_only_days_with_traffic(portal, runtime, make_account, make_wiki, login,
                                                          monkeypatch):
    owner = make_account()
    inst = make_wiki(owner, "quiet")
    client = portal.test_client()
    login(client, owner)
    today = utcnow().date()
    # A wiki reports every day of the period, zeros included.
    daily = [{"day": (today - timedelta(days=offset)).isoformat(), "request": 0, "page_view": 0, "error": 0}
             for offset in range(89, -1, -1)]
    daily[-4].update(request=5, page_view=3)
    monkeypatch.setattr(runtime, "analytics", lambda spec, days: _summary(daily))
    page = client.get(f"/instances/{inst['id']}/analytics?days=90").get_data(as_text=True)
    assert page.count('<td class="date">') == 1
    assert f'<td class="date">{daily[-4]["day"]}</td>' in page
    assert "No traffic in this period." not in page
    daily[-4].update(request=0, page_view=0)
    page = client.get(f"/instances/{inst['id']}/analytics?days=90").get_data(as_text=True)
    assert '<td class="date">' not in page
    assert '<td colspan="4" class="muted">No traffic in this period.</td>' in page


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_new_strings_of_the_recheck_exist_in_every_language(language):
    catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
    assert catalogue.get("hosting.analytics.no_traffic")


# ── Tables on phones ──────────────────────────────────────────────────────────


def test_the_feature_request_history_stacks_on_phones(portal, make_account, make_wiki, login):
    owner = make_account()
    inst = make_wiki(owner, "asking")
    client = portal.test_client()
    login(client, owner)
    client.post(f"/instances/{inst['id']}/features/public_access/requests",
                data={"reason": "Our documentation should be open to everyone."})
    page = client.get(f"/instances/{inst['id']}").get_data(as_text=True)
    history = re.search(r'<h3 class="feature-history__title">.*?</table>', page, re.S).group(0)
    # One block per request on phones, its Cancel button under it instead of off-screen.
    assert '<table class="table table--stack">' in history
    assert 'data-label="Status"' in history and 'data-label="Note"' in history
    assert "Cancel request" in history


def test_the_admin_wiki_list_puts_the_owner_under_the_name_on_small_phones(portal, make_account, make_wiki,
                                                                          login):
    admin, owner = make_account(admin=True), make_account("wikiowner")
    make_wiki(owner, "listed")
    client = portal.test_client()
    login(client, admin)
    page = client.get("/admin").get_data(as_text=True)
    assert '<th class="col-owner">Owner</th>' in page and '<td class="col-owner">wikiowner</td>' in page
    assert re.search(r'>listed</a>(?:(?!</td>).)*<span class="small muted cell-note owner-note">'
                     r'<span class="visually-hidden">Owner: </span>wikiowner</span></td>', page, re.S)
    css = (HOSTING / "static" / "css" / "hosting.css").read_text(encoding="utf-8")
    assert ".cell-note.owner-note { display: none; }" in css
    assert re.search(r"@media \(max-width: 400px\) \{\s*\.table \.col-owner \{ display: none; \}\s*"
                     r"\.cell-note\.owner-note \{ display: block; \}", css)


# ── Help that names what the screens show ─────────────────────────────────────


def _feature_names(language: str) -> set[str]:
    names: set[str] = set()
    for path in PACKAGE.rglob(f"translations/{language}.json"):
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        names |= {value for key, value in catalogue.items() if re.fullmatch(r"feature\.[a-z_]+\.name", key)}
    return names


def _article(language: str, slug: str) -> str:
    articles = json.loads((HOSTING / "content" / f"help.{language}.json").read_text(encoding="utf-8"))
    return " ".join(section["html"] for section in next(a for a in articles if a["slug"] == slug)["sections"])


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_the_plugin_list_uses_the_names_of_the_wiki_plugins_page(language):
    listed = re.findall(r"<li><strong>([^<]+)</strong>", _article(language, "plugins"))
    assert listed and not set(listed) - _feature_names(language)
    assert listed == sorted(listed, key=str.casefold)
    governance = next(name for name in _feature_names(language)
                      if name in ("Page governance", "Protezione e prenotazioni delle pagine",
                                  "Seitenschutz und Reservierungen"))
    assert f"<strong>{governance}</strong>" in _article(language, "features")


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_account_help_names_the_wiki_page_sections(language):
    catalogue = json.loads((HOSTING / "translations" / f"{language}.json").read_text(encoding="utf-8"))
    article = _article(language, "account-management")
    for key in ("hosting.instance.reset_or_delete", "hosting.instance.terminate"):
        assert f"<strong>{catalogue[key]}</strong>" in article, key
    assert "Reset or delete wiki" not in article and "Reimposta o elimina wiki" not in article
    assert "Wiki zurücksetzen oder löschen" not in article
