"""Portal formatting: dates, durations, sizes, platform reasons, event labels and the maintenance gate."""

from __future__ import annotations

import re
from pathlib import Path

from bananawiki.hosting import i18n, templating
from bananawiki.hosting.app import _MaintenanceGate

PACKAGE = Path(__file__).resolve().parents[2] / "bananawiki"
HOSTING = PACKAGE / "hosting"
MB = 1024 * 1024
STORAGE_REASON = "Automatic storage protection: 130.0 MB used, 100 MB allowed."


def _in(portal, lang: str):
    """A request context in *lang* for calling the template helpers directly."""
    context = portal.test_request_context("/", headers={"Accept-Language": lang})
    return context


# Dates, durations and sizes


def test_durations_use_the_units_of_the_interface_language(portal):
    with _in(portal, "it"):
        assert templating.duration(3 * 86400 + 4 * 3600) == "3 g 4 h"
        assert templating.duration(400 * 86400) == "1 a 35 g"
        assert templating.duration(2 * 3600 + 5 * 60) == "2 h 5 min"
        assert templating.duration_long(2 * 86400 + 23 * 3600) == "2 giorni e 23 ore"
        assert templating.duration_long(86400) == "1 giorno"
    with _in(portal, "en"):
        assert templating.duration(3 * 86400 + 4 * 3600) == "3d 4h"
        assert templating.duration_long(3600 + 60) == "1 hour and 1 minute"


# Reasons and events


def test_reasons_the_platform_writes_are_shown_in_the_visitors_language(portal):
    with _in(portal, "it"):
        # A number and its unit never part at a line break (no-break space).
        assert templating.server_text(STORAGE_REASON) == \
            "Protezione automatica dello spazio: 130\u00a0MB occupati su 100\u00a0MB consentiti."
        detail = ("its files take more than the 110 MiB its storage limit allows in a backup (a sparse file at its "
                  "full size); stopped at uploads/raw/IMG_2041.CR3")
        assert templating.server_text(detail).startswith("Interrotto a uploads/raw/IMG_2041.CR3")
        assert "Automatic" not in templating.server_text("Automatic storage protection: 5.0 MB used, 1 MB allowed.")
        # An administrator's own words are shown as written.
        assert templating.server_text("Repeated spam") == "Repeated spam"
        assert templating.event_label("instance.suspend") == "Sospesa"
        assert templating.event_label("instance.terminated.something_new") == "Chiusa"
        assert templating.event_label("unknown.code") == "unknown.code"


def test_every_event_the_portal_records_has_a_label_in_each_language(portal):
    """The moderation log shows a label, never the raw code, for every event the portal records."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in HOSTING.rglob("*.py"))
    codes = set(re.findall(r'events\.record\([^,]+, [^,]+, "([a-z_.]+)"[,)]', source))
    # Codes built from a literal and a choice: "account.admin." + ("granted" if promote else "revoked"), ...
    for prefix, first, second in re.findall(
            r'events\.record\([^,]+, [^,]+, "([a-z_.]+)" \+ \("([a-z_]+)" if [^"]+ else "([a-z_]+)"\)', source):
        codes |= {prefix + first, prefix + second}
    assert {"plugins.quarantined", "storage.over_plan", "account.admin.granted"} <= codes and len(codes) > 40
    for language in i18n.LANGUAGES:
        with _in(portal, language):
            assert [code for code in sorted(codes) if templating.event_label(code) == code] == [], language


# Maintenance page


def test_the_maintenance_gate_answers_browsers_in_their_language():
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)
    seen: dict = {}

    def start(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(gate({"PATH_INFO": "/dashboard", "HTTP_ACCEPT": "text/html,*/*",
                          "HTTP_ACCEPT_LANGUAGE": "it-IT,it;q=0.9"}, start)).decode()
    assert seen["status"].startswith("503") and seen["headers"]["Content-Type"].startswith("text/html")
    assert '<html lang="it">' in body and "Manutenzione in corso" in body and "Maintenance" not in body
    assert seen["headers"]["Retry-After"] and seen["headers"]["Cache-Control"] == "no-store"
    english = b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html"}, start)).decode()
    assert '<html lang="en">' in english and "Maintenance in progress" in english
    plain = b"".join(gate({"PATH_INFO": "/api/v1/instances", "HTTP_ACCEPT": "application/json"}, start))
    assert seen["headers"]["Content-Type"].startswith("text/plain") and b"Maintenance" in plain


def test_the_maintenance_gate_has_a_page_in_every_portal_language():
    """German too; a bare ``*`` (no preference) gets English, not the first language by code."""
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)

    def page(**environ) -> str:
        return b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", **environ},
                             lambda status, headers: None)).decode()

    for language in i18n.LANGUAGES:
        assert f'<html lang="{language}">' in page(HTTP_ACCEPT_LANGUAGE=f"{language};q=1")
        assert f'<html lang="{language}">' in page(HTTP_COOKIE=f"bw_lang={language}")
    assert '<html lang="en">' in page(HTTP_ACCEPT_LANGUAGE="*")
    assert '<html lang="en">' in page(HTTP_ACCEPT_LANGUAGE="de, en")  # a tie that includes English


def test_the_maintenance_page_is_dark_unless_the_visitor_chose_light():
    gate = _MaintenanceGate(lambda environ, start: [b"app"], __file__)

    def page(**environ) -> str:
        return b"".join(gate({"PATH_INFO": "/", "HTTP_ACCEPT": "text/html", **environ},
                             lambda status, headers: None)).decode()

    assert ":root{--page:#0f151c;" in page() and "prefers-color-scheme" not in page()
    assert '<html data-theme="light" lang="en">' in page(HTTP_COOKIE="bw_theme=light")
