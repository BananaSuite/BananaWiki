"""Tests for the admin language pack manager (download / upload / delete)."""

import io
import json

import pytest


@pytest.fixture(autouse=True)
def _restore_translations_cache():
    """Ensure the translation LRU cache is cleared before and after each test."""
    from helpers import _translations as tr_mod
    tr_mod.reload_translations()
    yield
    tr_mod.reload_translations()


def test_admin_languages_page_lists_builtin(client, logged_in_admin):
    """Admin can view the language manager page and see built-in packs."""
    resp = client.get("/admin/interface-languages")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "English" in body
    assert "Italiano" in body


def test_admin_languages_requires_login(client):
    """Anonymous users are redirected away from the language manager."""
    resp = client.get("/admin/interface-languages", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_download_builtin_language_returns_json(client, logged_in_admin):
    """Downloading 'en' returns a JSON file with a _meta block."""
    resp = client.get("/admin/interface-languages/en/download")
    assert resp.status_code == 200
    assert resp.mimetype in ("application/json", "application/x-json")
    assert resp.headers.get("Content-Disposition", "").startswith("attachment")
    data = json.loads(resp.get_data(as_text=True))
    assert "_meta" in data
    assert data["_meta"]["code"] == "en"


def test_download_unknown_language_redirects(client, logged_in_admin):
    """Downloading a missing language redirects with an error flash."""
    resp = client.get("/admin/interface-languages/zz/download", follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_upload_valid_language_pack(client, logged_in_admin, tmp_path, monkeypatch):
    """Uploading a JSON file with a valid _meta block creates the pack."""
    # Redirect translations to a temp dir so we don't pollute the repo.
    from helpers import _translations as tr_mod
    monkeypatch.setattr(tr_mod, "_TRANSLATIONS_DIR", str(tmp_path))
    # Seed with an empty 'en' so fallback is satisfied.
    (tmp_path / "en.json").write_text(
        json.dumps({"_meta": {"code": "en", "name": "English"}}),
        encoding="utf-8",
    )
    tr_mod.reload_translations()

    payload = {
        "_meta": {
            "code": "tt",
            "name": "Test Lang",
            "english_name": "Test",
            "rtl": False,
            "author": "Tester",
        },
        "common.save": "Sàlva",
    }
    data = {
        "csrf_token": "test",
        "language_file": (
            io.BytesIO(json.dumps(payload).encode("utf-8")),
            "tt.json",
        ),
    }
    resp = client.post(
        "/admin/interface-languages/upload",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert (tmp_path / "tt.json").exists()
    saved = json.loads((tmp_path / "tt.json").read_text(encoding="utf-8"))
    assert saved["_meta"]["code"] == "tt"
    assert saved["common.save"] == "Sàlva"


def test_upload_rejects_missing_meta(client, logged_in_admin, tmp_path, monkeypatch):
    """Uploads without a _meta block are rejected."""
    from helpers import _translations as tr_mod
    monkeypatch.setattr(tr_mod, "_TRANSLATIONS_DIR", str(tmp_path))
    tr_mod.reload_translations()
    data = {
        "csrf_token": "test",
        "language_file": (
            io.BytesIO(b'{"common.save":"X"}'),
            "broken.json",
        ),
    }
    resp = client.post(
        "/admin/interface-languages/upload",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert not (tmp_path / "broken.json").exists()


def test_delete_builtin_language_file_protected(client, logged_in_admin, tmp_path, monkeypatch):
    """Built-in 'en' and 'it' language files cannot be deleted."""
    from helpers import _translations as tr_mod
    monkeypatch.setattr(tr_mod, "_TRANSLATIONS_DIR", str(tmp_path))
    en_path = tmp_path / "en.json"
    en_path.write_text(json.dumps({"_meta": {"code": "en", "name": "English"}}), encoding="utf-8")
    tr_mod.reload_translations()

    resp = client.post(
        "/admin/interface-languages/en/delete-file",
        data={"csrf_token": "test"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert en_path.exists(), "Built-in 'en' file must not be deleted"


def test_delete_custom_language_file(client, logged_in_admin, tmp_path, monkeypatch):
    """Custom language files can be deleted."""
    from helpers import _translations as tr_mod
    monkeypatch.setattr(tr_mod, "_TRANSLATIONS_DIR", str(tmp_path))
    custom_path = tmp_path / "fr.json"
    custom_path.write_text(
        json.dumps({"_meta": {"code": "fr", "name": "Français"}}),
        encoding="utf-8",
    )
    tr_mod.reload_translations()

    resp = client.post(
        "/admin/interface-languages/fr/delete-file",
        data={"csrf_token": "test"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert not custom_path.exists()


def test_t_skips_meta_key():
    """The t() helper must not return _meta as a translation."""
    from helpers import t
    # Even if a JSON file contains a _meta block, t('_meta') falls back to key.
    assert t("_meta") == "_meta"
    assert t("_meta.code") == "_meta.code"


def test_validate_language_payload_normalizes_meta():
    """Validator normalizes _meta and rejects missing fields."""
    from helpers import validate_language_payload

    data, error = validate_language_payload({
        "_meta": {"code": "ES", "name": "Español"},
        "common.save": "Guardar",
    })
    assert error is None
    assert data is not None
    assert data["_meta"]["code"] == "es"
    assert data["_meta"]["name"] == "Español"

    bad, err = validate_language_payload({"common.save": "x"})
    assert bad is None
    assert err  # error message present
