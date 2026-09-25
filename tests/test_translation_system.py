"""Tests for interface language defaults and per-user overrides."""

import db


def test_admin_can_add_custom_interface_language(logged_in_admin):
    """Admins can add custom interface languages from site settings."""
    resp = logged_in_admin.post(
        "/admin/interface-languages/add",
        data={
            "language_code": "es",
            "language_name": "Español",
            "language_enabled": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert '"es"' in settings["interface_languages_json"]


def test_admin_settings_updates_default_interface_language(logged_in_admin):
    """Admin settings should persist the default interface language."""
    db.update_site_settings(interface_languages_json='{"es":{"name":"Español","enabled":true}}')
    resp = logged_in_admin.post(
        "/global-settings",
        data={
            "site_name": "BananaWiki",
            "timezone": "UTC",
            "interface_language": "es",
            "interface_language_fallback": "en",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert db.get_site_settings()["interface_language"] == "es"


def test_accessibility_api_persists_interface_language_preference(logged_in_admin, admin_user):
    """Users can save their personal interface language via accessibility API."""
    db.update_site_settings(interface_languages_json='{"es":{"name":"Español","enabled":true}}')
    resp = logged_in_admin.post(
        "/api/accessibility",
        json={"interface_language": "es"},
    )
    assert resp.status_code == 200
    prefs = db.get_user_accessibility(admin_user)
    assert prefs["interface_language"] == "es"


def test_user_language_override_takes_precedence(logged_in_admin, admin_user):
    """User language preference should override the site default in rendered HTML."""
    db.update_site_settings(interface_language="it")
    prefs = dict(db._A11Y_DEFAULTS)
    prefs["interface_language"] = "en"
    db.save_user_accessibility(admin_user, prefs)

    resp = logged_in_admin.get("/")
    assert resp.status_code == 200
    assert b'<html lang="en"' in resp.data
    assert b'data-site-lang="it"' in resp.data


def test_disabling_custom_language_resets_site_default(logged_in_admin):
    """Disabling a custom default language should reset to fallback."""
    db.update_site_settings(
        interface_languages_json='{"es":{"name":"Español","enabled":true}}',
        interface_language="es",
        interface_language_fallback="it",
    )
    resp = logged_in_admin.post(
        "/admin/interface-languages/es/toggle",
        data={"enabled": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    settings = db.get_site_settings()
    assert settings["interface_language"] == "it"
    resp_home = logged_in_admin.get("/")
    assert b'<html lang="it"' in resp_home.data


def test_default_user_pref_follows_site_setting_over_browser(logged_in_admin, admin_user):
    """A user whose customization is "default" must follow the site setting.

    Even when the browser sends ``Accept-Language: it``, the user has
    not chosen a language and the site is configured for English, so
    English should win.  This is the regression test for the bug where
    the browser language was overriding the admin-configured site
    default.
    """
    db.update_site_settings(interface_language="en")
    prefs = dict(db._A11Y_DEFAULTS)
    prefs["interface_language"] = "default"
    db.save_user_accessibility(admin_user, prefs)

    resp = logged_in_admin.get(
        "/",
        headers={"Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    assert resp.status_code == 200
    assert b'<html lang="en"' in resp.data


def test_guest_with_default_pref_follows_site_setting_over_browser(client):
    """Same as above but for guests (no logged-in user)."""
    db.update_site_settings(setup_done=1, interface_language="en")
    resp = client.get(
        "/",
        headers={"Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    assert resp.status_code in (200, 302)
    if resp.status_code == 200:
        assert b'<html lang="en"' in resp.data


def test_t_default_kwarg_used_when_key_missing():
    """t('missing.key', default='foo') returns 'foo' for unknown keys."""
    from helpers import t
    out = t(
        "test.this.key.does.not.exist.anywhere",
        default="Sensible fallback shown to the user",
    )
    assert out == "Sensible fallback shown to the user"


def test_t_default_kwarg_ignored_when_key_present():
    """t() must still return the translation when one exists, even with default."""
    from helpers import t
    # ``admin.settings.title`` exists in en.json (covers the admin site
    # settings header) so the default must be ignored.
    out = t("admin.settings.title", default="WRONG FALLBACK")
    assert out != "WRONG FALLBACK"
    assert out != "admin.settings.title"


def test_t_default_kwarg_combines_with_format_kwargs():
    """Format kwargs continue to work alongside a default fallback."""
    from helpers import t
    out = t(
        "missing.format.key.xyz",
        default="Hello {name}",
        name="World",
    )
    assert out == "Hello World"


def test_t_default_non_string_falls_back_to_key():
    """Non-string default values must not break the helper."""
    from helpers import t
    out = t("missing.key.zzz", default=None)
    assert out == "missing.key.zzz"


def test_settings_template_translation_keys_present():
    """Regression: every t() key in admin/settings.html resolves in en.json.

    The settings page previously rendered raw keys like
    ``admin.settings.dm_auto_clear_requires_cleanup_plugin`` because the
    translation files were missing entries.  Catch any new occurrences
    automatically.
    """
    import json
    import os
    import re

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    settings_html = os.path.join(base, "app", "templates", "admin", "settings.html")
    with open(settings_html, encoding="utf-8") as f:
        body = f.read()

    # Match only proper dotted translation keys (skip plain identifiers
    # like ``t('a')`` that appear in JavaScript or non-translation calls).
    pattern = re.compile(
        r"""t\(\s*['\"]([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)['\"]"""
    )
    keys = set(pattern.findall(body))

    with open(os.path.join(base, "translations", "en.json"), encoding="utf-8") as f:
        en = json.load(f)

    missing = sorted(k for k in keys if k not in en or not isinstance(en[k], str))
    assert not missing, (
        "Missing translation keys in translations/en.json for admin/settings.html: "
        + ", ".join(missing)
    )


def test_static_translation_keys_present_in_builtin_packs():
    """Every statically referenced translation key resolves in en.json and it.json."""
    import ast
    import json
    import os
    import pathlib
    import re

    base = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    keys = set()

    def add_python_keys(path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            is_t_call = isinstance(func, ast.Name) and func.id == "t"
            if not is_t_call:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                key = first.value
                if "." in key and not key.endswith("."):
                    keys.add(key)

    py_roots = [
        base / "app.py",
        base / "helpers",
        base / "routes",
        base / "hosting",
        base / "plugins" / "builtin",
    ]
    for root in py_roots:
        paths = [root] if root.is_file() else root.rglob("*.py")
        for path in paths:
            if "__pycache__" not in path.parts:
                add_python_keys(path)

    call_pattern = re.compile(
        r"""(?:\bt|_t|BW\.t|window\._t)\(\s*['"]([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)['"]"""
    )
    text_roots = [
        base / "app" / "templates",
        base / "hosting" / "templates",
        base / "app" / "static" / "js",
        base / "plugins" / "builtin",
    ]
    for root in text_roots:
        for path in root.rglob("*"):
            if path.suffix not in {".html", ".js"}:
                continue
            body = path.read_text(encoding="utf-8")
            for match in call_pattern.finditer(body):
                key = match.group(1)
                # Skip dynamic prefixes such as t('admin.announcements.color.' + c).
                if body[match.end():match.end() + 8].lstrip().startswith("+"):
                    continue
                keys.add(key)

    onboarding_feature_ids = [
        "kanban", "canvas", "drafts", "page_governance", "chat",
        "attachments", "custom_pages", "assessments", "user_profiles",
        "tts", "api_service", "deletion_slowdown", "temporary_accounts",
    ]
    for plugin_id in onboarding_feature_ids:
        keys.add(f"onboarding.feature.{plugin_id}.name")
        keys.add(f"onboarding.feature.{plugin_id}.description")

    onboarding_tour_steps = [
        "home", "search", "personal_settings", "create_page",
        "quick_create", "kanban", "canvas", "admin_settings",
        "admin_users", "admin_plugins",
    ]
    for step_id in onboarding_tour_steps:
        keys.add(f"onboarding.tour.{step_id}.title")
        keys.add(f"onboarding.tour.{step_id}.body")
        keys.add(f"onboarding.tour.{step_id}.target_label")

    for lang in ("en", "it"):
        with open(base / "translations" / f"{lang}.json", encoding="utf-8") as f:
            pack = json.load(f)
        missing = sorted(k for k in keys if k not in pack or not isinstance(pack[k], str))
        assert not missing, (
            f"Missing translation keys in translations/{lang}.json: "
            + ", ".join(missing)
        )


def test_builtin_translation_packs_have_matching_string_keys():
    """English and Italian built-in packs should expose the same strings."""
    import json
    import os

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "translations", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(base, "translations", "it.json"), encoding="utf-8") as f:
        it = json.load(f)

    en_keys = {
        key for key, value in en.items()
        if key != "_meta" and isinstance(value, str)
    }
    it_keys = {
        key for key, value in it.items()
        if key != "_meta" and isinstance(value, str)
    }

    assert sorted(en_keys - it_keys) == []
    assert sorted(it_keys - en_keys) == []
