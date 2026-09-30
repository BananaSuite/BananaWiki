"""Interface languages: switches, default, uploaded files and their validation."""

from __future__ import annotations

import io
import json
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
    admin_client.post("/admin/interface-languages/it/toggle", data={"enabled": "0"})
    admin_client.post("/admin/interface-languages/en/toggle", data={"enabled": "0"})
    assert switches(db).get("en", {"enabled": True})["enabled"] is True


def test_legacy_format_is_understood(app, admin_client, db):
    upload(admin_client, pack())
    db.execute("UPDATE site_settings SET interface_languages_json = ? WHERE id = 1",
               (json.dumps({"fr": {"name": "Français", "enabled": False}}),))
    assert set(enabled(app)) == {"en", "it"}


def test_builtin_override_and_download(app, admin_client):
    upload(admin_client, pack(code="it", **{"common.save": "Salva adesso"}), name="it.json")
    assert "Salva adesso" in admin_client.get("/admin/interface-languages?lang=it").get_data(as_text=True)
    download = admin_client.get("/admin/interface-languages/it/download")
    assert json.loads(download.data)["common.save"] == "Salva adesso"
    admin_client.post("/admin/interface-languages/it/delete-file")
    assert json.loads(admin_client.get("/admin/interface-languages/it/download").data)["common.save"] != "Salva adesso"
    assert admin_client.get("/admin/interface-languages/xx/download").status_code == 404
