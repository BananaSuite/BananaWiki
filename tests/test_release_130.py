import os


def test_hosting_schema_release_130_fields(tmp_path, monkeypatch):
    from hosting import config as hosting_config
    monkeypatch.setattr(hosting_config, "HOSTING_DATABASE_PATH", str(tmp_path / "hosting.db"))
    from hosting.db import init_hosting_db, get_hosting_db_context, get_hosting_settings
    init_hosting_db()
    with get_hosting_db_context() as conn:
        account_cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        instance_cols = {row[1] for row in conn.execute("PRAGMA table_info(instances)")}
    assert {"email", "terms_accepted_at", "terms_version", "deleted_at"} <= account_cols
    assert {"declared_use_case", "tos_compliance_declared_at"} <= instance_cols
    settings = get_hosting_settings()
    assert settings["ask_email_new_signup"] == 0
    assert settings["ask_email_existing_users"] == 0
    assert settings["email_required"] == 0
    assert settings["email_verification_required"] == 0
    assert settings["forbid_non_admin_public_wikis"] == 1


def test_managed_hosting_environment_closes_external_plugins(tmp_path, monkeypatch):
    import config
    import plugin_loader
    monkeypatch.setattr(config, "ALLOW_EXTERNAL_PLUGINS", False)
    archive = tmp_path / "anything.bwplugin"
    archive.write_bytes(b"not even a zip")
    try:
        plugin_loader.import_bwplugin(str(archive))
    except Exception as exc:
        assert "disabled on managed" in str(exc).lower()
    else:
        raise AssertionError("managed hosting accepted an external plugin")
