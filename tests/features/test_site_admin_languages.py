"""Interface languages: switches, default, uploaded files and their validation."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from bananawiki.wiki import i18n

REPO = Path(__file__).parents[2]


def pack(code="fr", **strings):
    data = {"_meta": {"code": code, "name": "Français", "author": "Ana"}, "common.save": "Enregistrer"}
    data.update(strings)
    return data


def upload(client, payload, name="fr.json"):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post("/admin/interface-languages/upload", data={"language_file": (io.BytesIO(raw), name)},
                       content_type="multipart/form-data")


def switches(db):
    return json.loads(db.scalar("SELECT interface_languages_json FROM site_settings WHERE id = 1"))


def enabled(app):
    with app.test_request_context():
        from bananawiki.wiki.db import connection_scope

        with connection_scope():
            return i18n.enabled_languages()


def test_upload_installs_into_the_instance_directory(app, admin_client, db):
    assert upload(admin_client, pack(**{"not.a.key": "x"})).status_code == 302
    stored = Path(app.config["BW"].instance_dir) / "translations" / "fr.json"
    assert stored.is_file() and not (REPO / "bananawiki" / "wiki" / "translations" / "fr.json").exists()
    data = json.loads(stored.read_text(encoding="utf-8"))
    assert data["common.save"] == "Enregistrer" and "not.a.key" not in data
    assert switches(db)["fr"] == {"name": "Français", "enabled": True}
    assert "fr" in enabled(app)
    assert "Enregistrer" in admin_client.get("/admin/interface-languages?lang=fr").get_data(as_text=True)


def test_placeholders_must_match_english(app, admin_client, db):
    english = json.loads((REPO / "bananawiki/wiki/features/site_admin/translations/en.json").read_text())
    key = "site_admin.error.number"
    assert "{minimum}" in english[key]
    for value in ("Entre {minimum} et {max}", "{minimum.__class__} {maximum}", "Aucun"):
        page = upload(admin_client, pack(**{key: value}))
        assert page.status_code == 302
    assert not (Path(app.config["BW"].instance_dir) / "translations" / "fr.json").exists()
    upload(admin_client, pack(**{key: "Entre {minimum} et {maximum}"}))
    assert (Path(app.config["BW"].instance_dir) / "translations" / "fr.json").exists()


def test_invalid_files_are_refused(app, admin_client):
    for payload in (b"not json", b"[]", json.dumps({"common.save": "x"}).encode(),
                    json.dumps(pack(code="../../etc")).encode(), json.dumps(pack(**{"common.cancel": 3})).encode(),
                    json.dumps({"_meta": {"code": "fr", "name": "F"}}).encode(), b"x" * (3 * 1024 * 1024)):
        upload(admin_client, payload)
    folder = Path(app.config["BW"].instance_dir) / "translations"
    assert not list(folder.glob("*.json"))


def test_toggle_default_and_delete(app, admin_client, db):
    upload(admin_client, pack())
    admin_client.post("/admin/interface-languages/it/toggle", data={"enabled": "0"})
    assert switches(db)["it"]["enabled"] is False and "it" not in enabled(app)
    admin_client.post("/admin/interface-languages/default",
                      data={"interface_language": "fr", "interface_language_fallback": "en"})
    assert db.scalar("SELECT interface_language FROM site_settings") == "fr"
    admin_client.post("/admin/interface-languages/fr/toggle", data={"enabled": "0"})
    assert switches(db)["fr"]["enabled"] is True  # the default cannot be switched off
    admin_client.post("/admin/interface-languages/fr/delete-file")
    assert "fr" in switches(db)  # nor deleted
    admin_client.post("/admin/interface-languages/default", data={"interface_language": "it"})
    assert db.scalar("SELECT interface_language FROM site_settings") == "fr"  # "it" is disabled
    admin_client.post("/admin/interface-languages/default",
                      data={"interface_language": "en", "interface_language_fallback": "en"})
    admin_client.post("/admin/interface-languages/fr/delete")
    assert "fr" not in switches(db)
    assert not (Path(app.config["BW"].instance_dir) / "translations" / "fr.json").exists()


def test_the_last_language_stays_enabled(admin_client, db):
    for code in sorted(set(i18n.BUILTIN_LANGUAGES) - {"en"}):
        admin_client.post(f"/admin/interface-languages/{code}/toggle", data={"enabled": "0"})
    admin_client.post("/admin/interface-languages/en/toggle", data={"enabled": "0"})
    assert switches(db).get("en", {"enabled": True})["enabled"] is True


def test_legacy_format_is_understood(app, admin_client, db):
    upload(admin_client, pack())
    db.execute("UPDATE site_settings SET interface_languages_json = ? WHERE id = 1",
               (json.dumps({"fr": {"name": "Français", "enabled": False}}),))
    assert "fr" not in enabled(app) and {"en", "it"} <= set(enabled(app))


def test_builtin_override_and_download(app, admin_client):
    upload(admin_client, pack(code="it", **{"common.save": "Salva adesso"}), name="it.json")
    assert "Salva adesso" in admin_client.get("/admin/interface-languages?lang=it").get_data(as_text=True)
    download = admin_client.get("/admin/interface-languages/it/download")
    assert json.loads(download.data)["common.save"] == "Salva adesso"
    admin_client.post("/admin/interface-languages/it/delete-file")
    assert json.loads(admin_client.get("/admin/interface-languages/it/download").data)["common.save"] != "Salva adesso"
    assert admin_client.get("/admin/interface-languages/xx/download").status_code == 404


def test_every_builtin_language_can_be_the_documentation_language(admin_client, db):
    from bananawiki.wiki.features.api_service import settings_rules

    for code in i18n.BUILTIN_LANGUAGES:
        admin_client.post("/admin/interface-languages/default",
                          data={"interface_language": "en", "interface_language_fallback": code})
        assert db.scalar("SELECT interface_language_fallback FROM site_settings") == code
        assert settings_rules.RULES["interface_language_fallback"].parse(f" {code.upper()} ") == code


# ── German, built in ──────────────────────────────────────────────────────────

GERMAN_PACK = {"_meta": {"code": "de", "name": "Deutsch (Schule)", "author": "Ana"},
               "common.save": "Jetzt speichern"}


def german_uploaded_before_the_upgrade(admin_client, db, *, enabled: bool = True):
    """What uploading "de" left when German was not bundled: its file and a switch for it."""
    upload(admin_client, GERMAN_PACK, name="de.json")
    db.execute("UPDATE site_settings SET interface_languages_json = ? WHERE id = 1",
               (json.dumps({"de": {"name": "Deutsch (Schule)", "enabled": enabled}}),))


def rows(app):
    from bananawiki.wiki.db import connection_scope
    from bananawiki.wiki.features.site_admin import languages

    with app.test_request_context(), connection_scope():
        return {row["code"]: row for row in languages.overview()}


def html_lang(client, path, **headers):
    page = client.get(path, headers=headers).get_data(as_text=True)
    return re.search(r'<html lang="([a-z-]+)"', page).group(1)


def test_german_is_built_in_enabled_by_default_and_can_be_switched_off(app, admin_client, db):
    visitor = app.test_client()
    assert "de" in enabled(app) and "de" not in switches(db)
    assert rows(app)["de"]["builtin"] and not rows(app)["de"]["custom_file"]
    assert html_lang(app.test_client(), "/login", **{"Accept-Language": "de-CH, de;q=0.9, en;q=0.5"}) == "de"
    assert html_lang(visitor, "/login?lang=de") == html_lang(visitor, "/login") == "de"  # the choice is kept
    admin_client.post("/admin/interface-languages/de/toggle", data={"enabled": "0"})
    assert switches(db)["de"]["enabled"] is False and "de" not in enabled(app)
    assert html_lang(visitor, "/login?lang=de") == "en"
    assert html_lang(app.test_client(), "/login", **{"Accept-Language": "de"}) == "en"


def test_a_wildcard_accept_language_gets_the_site_default(app, db):
    assert html_lang(app.test_client(), "/login", **{"Accept-Language": "*"}) == "en"
    db.execute("UPDATE site_settings SET interface_language = 'it' WHERE id = 1")
    assert html_lang(app.test_client(), "/login", **{"Accept-Language": "*"}) == "it"
    assert html_lang(app.test_client(), "/login", **{"Accept-Language": "en, *;q=0.1"}) == "en"


def test_german_uploaded_before_the_upgrade_overrides_the_bundled_one(app, admin_client, db):
    from bananawiki.core.i18n import Catalog

    german_pack_path = Path(app.config["BW"].instance_dir) / "translations" / "de.json"
    german_uploaded_before_the_upgrade(admin_client, db)
    with app.test_request_context():
        bundled = Catalog(i18n.catalog().bundled_dirs).strings("de")
    assert bundled and german_pack_path.is_file()
    row = rows(app)["de"]
    assert row["builtin"] and row["custom_file"] and row["enabled"] and row["name"] == "Deutsch (Schule)"
    admin_client.post("/admin/interface-languages/default",
                      data={"interface_language": "de", "interface_language_fallback": "de"})
    assert db.scalar("SELECT interface_language FROM site_settings") == "de"
    assert "Jetzt speichern" in admin_client.get("/admin/interface-languages").get_data(as_text=True)
    with app.test_request_context():
        merged = i18n.catalog().strings("de")
    assert merged["common.save"] == "Jetzt speichern"
    assert all(merged[key] == text for key, text in bundled.items() if key not in GERMAN_PACK)
    # Removing the upload, even for the default language, leaves the bundled German and its switch.
    admin_client.post("/admin/interface-languages/de/delete-file")
    assert not german_pack_path.exists()
    assert db.scalar("SELECT interface_language FROM site_settings") == "de"
    assert switches(db)["de"]["enabled"] is True and "de" in enabled(app)
    assert rows(app)["de"]["name"] == "Deutsch" and not rows(app)["de"]["custom_file"]
    with app.test_request_context():
        assert i18n.catalog().strings("de")["common.save"] == bundled["common.save"] != "Jetzt speichern"


def test_a_switch_saved_for_an_uploaded_german_still_applies(app, admin_client, db):
    german_uploaded_before_the_upgrade(admin_client, db, enabled=False)
    assert "de" not in enabled(app)  # switched off before the upgrade, so it stays off
    admin_client.post("/admin/interface-languages/de/toggle", data={"enabled": "1"})
    assert switches(db)["de"] == {"name": "Deutsch (Schule)", "enabled": True} and "de" in enabled(app)


def test_uploading_german_now_adds_an_override_without_a_switch(app, admin_client, db):
    upload(admin_client, GERMAN_PACK, name="de.json")
    assert "de" not in switches(db) and "de" in enabled(app)
    assert "Jetzt speichern" in admin_client.get("/admin/interface-languages?lang=de").get_data(as_text=True)
