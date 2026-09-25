"""Tests for color customization presets feature."""

import db


# ── The nine preset palettes (mirrors main.js _colorPresets) ──────────────

PRESETS = {
    "ocean": {
        "custom_bg": "#0b1a2e", "custom_text": "#c8ddf0",
        "custom_primary": "#5b9bd5", "custom_secondary": "#112640",
        "custom_accent": "#a3c4f3", "custom_sidebar": "#091526",
    },
    "forest": {
        "custom_bg": "#0f1e12", "custom_text": "#c8dcc8",
        "custom_primary": "#4caf50", "custom_secondary": "#162a19",
        "custom_accent": "#8fbf9f", "custom_sidebar": "#0b180e",
    },
    "sunset": {
        "custom_bg": "#1f1017", "custom_text": "#f0ddd0",
        "custom_primary": "#e76f51", "custom_secondary": "#2a1520",
        "custom_accent": "#f4a261", "custom_sidebar": "#1a0d14",
    },
    "lavender": {
        "custom_bg": "#1a1428", "custom_text": "#d8d0e8",
        "custom_primary": "#9b7fd4", "custom_secondary": "#221a34",
        "custom_accent": "#c4b5e0", "custom_sidebar": "#151020",
    },
    "midnight": {
        "custom_bg": "#0a0a12", "custom_text": "#e0e0e8",
        "custom_primary": "#00d4ff", "custom_secondary": "#10101c",
        "custom_accent": "#7ee8ff", "custom_sidebar": "#08080e",
    },
    "copper": {
        "custom_bg": "#1c1410", "custom_text": "#e8dcd0",
        "custom_primary": "#b87333", "custom_secondary": "#241c16",
        "custom_accent": "#d4a574", "custom_sidebar": "#16100c",
    },
    "rose": {
        "custom_bg": "#1a0e14", "custom_text": "#f0d8e0",
        "custom_primary": "#e91e8c", "custom_secondary": "#240a18",
        "custom_accent": "#f48fb1", "custom_sidebar": "#140a10",
    },
    "slate": {
        "custom_bg": "#0e1220", "custom_text": "#d0d8f0",
        "custom_primary": "#7c8fc4", "custom_secondary": "#141826",
        "custom_accent": "#a8b8e0", "custom_sidebar": "#0a0e1a",
    },
    "ember": {
        "custom_bg": "#1a0e08", "custom_text": "#f0e0d0",
        "custom_primary": "#ff6b2b", "custom_secondary": "#221208",
        "custom_accent": "#ffad7a", "custom_sidebar": "#140a04",
    },
}


LIGHT_PRESETS = {
    "ocean": {
        "custom_bg": "#f3f8fd", "custom_text": "#17324a",
        "custom_primary": "#256fa8", "custom_secondary": "#ffffff",
        "custom_accent": "#4f90c7", "custom_sidebar": "#dbeaf7",
    },
    "forest": {
        "custom_bg": "#f2f8f1", "custom_text": "#1f3a24",
        "custom_primary": "#2f7d34", "custom_secondary": "#ffffff",
        "custom_accent": "#5c9a68", "custom_sidebar": "#dcebdd",
    },
    "sunset": {
        "custom_bg": "#fff4ed", "custom_text": "#513026",
        "custom_primary": "#c45136", "custom_secondary": "#ffffff",
        "custom_accent": "#de7d3c", "custom_sidebar": "#f5dfd4",
    },
    "lavender": {
        "custom_bg": "#f7f3fc", "custom_text": "#32274a",
        "custom_primary": "#7657b8", "custom_secondary": "#ffffff",
        "custom_accent": "#9a7acb", "custom_sidebar": "#e7def4",
    },
    "midnight": {
        "custom_bg": "#f2f6fb", "custom_text": "#202535",
        "custom_primary": "#157ea3", "custom_secondary": "#ffffff",
        "custom_accent": "#2ca7c9", "custom_sidebar": "#dce5ef",
    },
    "copper": {
        "custom_bg": "#fbf3ed", "custom_text": "#453028",
        "custom_primary": "#9c5f29", "custom_secondary": "#ffffff",
        "custom_accent": "#bd814c", "custom_sidebar": "#ecded4",
    },
    "rose": {
        "custom_bg": "#fff1f6", "custom_text": "#4c2636",
        "custom_primary": "#bf2f77", "custom_secondary": "#ffffff",
        "custom_accent": "#df6796", "custom_sidebar": "#f4d9e4",
    },
    "slate": {
        "custom_bg": "#f4f6fb", "custom_text": "#253044",
        "custom_primary": "#596b98", "custom_secondary": "#ffffff",
        "custom_accent": "#7688b4", "custom_sidebar": "#e0e5f0",
    },
    "ember": {
        "custom_bg": "#fff3ea", "custom_text": "#4f2d20",
        "custom_primary": "#c94f1d", "custom_secondary": "#ffffff",
        "custom_accent": "#e8793f", "custom_sidebar": "#f3dfd0",
    },
}


def test_all_preset_colors_accepted_by_api(logged_in_admin):
    """Every preset palette should be accepted by POST /api/accessibility."""
    for name, colors in PRESETS.items():
        resp = logged_in_admin.post("/api/accessibility", json=colors)
        assert resp.status_code == 200, f"Preset '{name}' rejected: {resp.data}"


def test_all_light_preset_colors_accepted_by_api(logged_in_admin):
    """Every light-mode preset palette should be accepted by POST /api/accessibility."""
    for name, colors in LIGHT_PRESETS.items():
        resp = logged_in_admin.post("/api/accessibility", json=colors)
        assert resp.status_code == 200, f"Light preset '{name}' rejected: {resp.data}"


def test_light_preset_colors_persisted(logged_in_admin):
    """Applying a light preset should persist all six custom_* keys."""
    preset = LIGHT_PRESETS["ocean"]
    resp = logged_in_admin.post("/api/accessibility", json=preset)
    assert resp.status_code == 200

    saved = logged_in_admin.get("/api/accessibility").get_json()
    for key, value in preset.items():
        assert saved[key] == value, f"{key}: expected {value}, got {saved[key]}"


def test_preset_colors_persisted(logged_in_admin):
    """Applying a preset should persist all six custom_* keys."""
    preset = PRESETS["ocean"]
    resp = logged_in_admin.post("/api/accessibility", json=preset)
    assert resp.status_code == 200

    resp = logged_in_admin.get("/api/accessibility")
    assert resp.status_code == 200
    saved = resp.get_json()
    for key, value in preset.items():
        assert saved[key] == value, f"{key}: expected {value}, got {saved[key]}"


def test_preset_then_custom_override(logged_in_admin):
    """After applying a preset, a single custom color change should work."""
    resp = logged_in_admin.post("/api/accessibility", json=PRESETS["forest"])
    assert resp.status_code == 200

    override = dict(PRESETS["forest"])
    override["custom_primary"] = "#ff0000"
    resp = logged_in_admin.post("/api/accessibility", json=override)
    assert resp.status_code == 200

    saved = logged_in_admin.get("/api/accessibility").get_json()
    assert saved["custom_primary"] == "#ff0000"
    assert saved["custom_bg"] == PRESETS["forest"]["custom_bg"]


def test_preset_cleared_by_reset(logged_in_admin):
    """Resetting accessibility should clear all preset colors."""
    logged_in_admin.post("/api/accessibility", json=PRESETS["sunset"])
    resp = logged_in_admin.post("/api/accessibility/reset", json={})
    assert resp.status_code == 200

    saved = logged_in_admin.get("/api/accessibility").get_json()
    for key in PRESETS["sunset"]:
        assert saved[key] == "", f"{key} should be empty after reset, got {saved[key]}"


def test_preset_panel_html_present(logged_in_admin):
    """The customization panel should contain the preset grid with all 9 presets."""
    resp = logged_in_admin.get("/")
    html = resp.data.decode()
    assert 'id="a11y-preset-grid"' in html
    for name in PRESETS:
        assert f'data-preset="{name}"' in html


def test_preset_colors_are_valid_hex():
    """All preset colors must be valid 7-character hex codes."""
    import re
    hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
    for name, colors in {**PRESETS, **{f"light-{k}": v for k, v in LIGHT_PRESETS.items()}}.items():
        for key, value in colors.items():
            assert hex_re.fullmatch(value), f"Preset '{name}' key '{key}' has invalid color: {value}"
