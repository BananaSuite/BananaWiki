"""Appearance: colours with theme files, and the site icon."""

from __future__ import annotations

import io
import json
import os

from PIL import Image

from bananawiki.wiki.features.site_admin.appearance import color_columns
from bananawiki.wiki.templating import THEME_DEFAULTS


def colors_form(**overrides):
    form = {column: "#123456" for column in color_columns()}
    form["default_theme_mode"] = "light"
    form.update(overrides)
    return form


def png_bytes(color=(255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload_icon(client, data=None, name="icon.png"):
    return client.post("/admin/settings/favicon/upload", data={"file": (io.BytesIO(data or png_bytes()), name)},
                       content_type="multipart/form-data")


def row(db):
    return db.one("SELECT * FROM site_settings WHERE id = 1")


def test_colours_are_saved_and_rendered(admin_client, db):
    assert admin_client.post("/admin/appearance/colors", data=colors_form()).status_code == 302
    assert row(db)["light_bg_color"] == "#123456" and row(db)["default_theme_mode"] == "light"
    assert "--bw-bg:#123456" in admin_client.get("/admin/appearance").get_data(as_text=True)


def test_invalid_colour_changes_nothing(admin_client, db):
    admin_client.post("/admin/appearance/colors", data=colors_form(primary_color="red;}body{x"))
    assert row(db)["primary_color"] == "#8fa0d4"


def test_reset_restores_defaults(admin_client, db):
    admin_client.post("/admin/appearance/colors", data=colors_form())
    admin_client.post("/admin/appearance/reset")
    assert row(db)["accent_color"] == THEME_DEFAULTS["dark"]["accent"]


def test_theme_file_round_trip(admin_client, db):
    admin_client.post("/admin/appearance/colors", data=colors_form(accent_color="#abcdef"))
    exported = admin_client.get("/global-settings/theme/export")
    assert exported.status_code == 200 and "bwtheme" in exported.headers["Content-Disposition"]
    payload = json.loads(exported.data)
    assert payload["theme"]["dark"]["accent"] == "#abcdef"
    admin_client.post("/admin/appearance/reset")
    response = admin_client.post("/global-settings/theme/import",
                                 data={"theme_file": (io.BytesIO(exported.data), "site.bwtheme")},
                                 content_type="multipart/form-data")
    assert response.status_code == 302
    assert row(db)["accent_color"] == "#abcdef"


def test_invalid_theme_files_are_refused(admin_client, db):
    bad = {"_meta": {"type": "bananawiki-theme", "format": "bwtheme"},
           "theme": {"default_theme_mode": "dark", "dark": {"primary": "javascript:"}, "light": {}}}
    for content, name in ((json.dumps(bad).encode(), "x.bwtheme"), (b"not json", "x.bwtheme"),
                          (b"{}", "x.txt"), (b" " * 200_000, "x.bwtheme")):
        admin_client.post("/admin/appearance/theme/import", data={"theme_file": (io.BytesIO(content), name)},
                          content_type="multipart/form-data")
    assert row(db)["primary_color"] == "#8fa0d4"


def test_favicon_upload_select_and_serve(app, admin_client, db):
    response = upload_icon(admin_client)
    assert response.status_code == 302
    settings = row(db)
    name = settings["favicon_custom"]
    assert name.startswith("custom_") and settings["favicon_type"] == "custom" and settings["favicon_enabled"] == 1
    assert json.loads(settings["favicon_order"]) == [name]
    assert os.path.isfile(os.path.join(app.config["BW"].folders.favicons, name))
    page = admin_client.get("/").get_data(as_text=True)
    assert f"/static/favicons/{name}" in page
    assert admin_client.get(f"/static/favicons/{name}").status_code == 200


def test_favicon_enabled_off_uses_the_default(admin_client, db):
    upload_icon(admin_client)
    admin_client.post("/admin/appearance/favicon/enabled", data={"favicon_enabled": "0"})
    assert "banana_yellow.png" in admin_client.get("/admin/appearance").get_data(as_text=True).split("<body")[0]


def test_non_images_are_refused(admin_client, db):
    response = upload_icon(admin_client, data=b"<svg onload=alert(1)>", name="evil.png")
    assert response.status_code == 302
    assert row(db)["favicon_custom"] == ""
    json_response = admin_client.post("/admin/settings/favicon/upload", headers={"Accept": "application/json"},
                                      data={"file": (io.BytesIO(b"GIF89a"), "x.svg")},
                                      content_type="multipart/form-data")
    assert json_response.status_code == 400 and json_response.get_json()["ok"] is False


def test_select_json_api_and_unknown_icons(admin_client, db):
    ok = admin_client.post("/global-settings/favicon/select", json={"favicon_type": "blue"})
    assert ok.get_json()["ok"] is True and row(db)["favicon_type"] == "blue"
    bad = admin_client.post("/admin/settings/favicon/select", json={"favicon_type": "custom",
                                                                    "favicon_custom": "custom_../../x"})
    assert bad.status_code == 400 and row(db)["favicon_type"] == "blue"


def test_delete_and_reorder(app, admin_client, db):
    upload_icon(admin_client)
    upload_icon(admin_client, png_bytes((0, 255, 0)))
    first, second = json.loads(row(db)["favicon_order"])
    admin_client.post("/admin/appearance/favicon/reorder", data={"filename": second, "direction": "up"})
    assert json.loads(row(db)["favicon_order"]) == [second, first]
    admin_client.post("/admin/settings/favicon/reorder", json={"order": [first, "custom_missing", second]})
    assert json.loads(row(db)["favicon_order"]) == [first, second]
    deleted = admin_client.post("/admin/settings/favicon/delete", json={"filename": second})
    assert deleted.get_json()["fallback_type"] == "yellow"
    assert row(db)["favicon_type"] == "yellow" and json.loads(row(db)["favicon_order"]) == [first]
    assert not os.path.exists(os.path.join(app.config["BW"].folders.favicons, second))
    assert admin_client.post("/admin/settings/favicon/delete", json={"filename": "custom_nope"}).status_code == 400


def test_restore_preset_selects_it(admin_client, db):
    admin_client.post("/admin/settings/restore-favicon/green")
    assert row(db)["favicon_type"] == "green"
    admin_client.post("/admin/settings/restore-favicon/../etc")
    assert row(db)["favicon_type"] == "green"
