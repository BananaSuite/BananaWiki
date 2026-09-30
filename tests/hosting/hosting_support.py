"""Helpers shared by the hosting portal tests."""

from __future__ import annotations

import itertools
import re
from typing import Any

from bananawiki.hosting.app import create_app
from bananawiki.hosting.config import load_config
from bananawiki.hosting.db import connection_scope
from bananawiki.hosting.runtime.fake import FakeRuntime

PASSWORD = "correct horse 42"
BOOTSTRAP = "bootstrap-token-for-tests"
counter = itertools.count(1)


def portal_environ(tmp_path, **extra: str) -> dict[str, str]:
    environ = {
        "HOSTING_ENV": "test",
        "HOSTING_DATABASE_PATH": str(tmp_path / "data" / "hosting.db"),
        "INSTANCES_DIR": str(tmp_path / "instances"),
        "HOSTING_IMPORT_TEMP_DIR": str(tmp_path / "tmp_imports"),
        "HOSTING_EXPORT_TEMP_DIR": str(tmp_path / "tmp_exports"),
        "HOSTING_BOOTSTRAP_TOKEN": BOOTSTRAP,
        "BW_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000",
        "BASE_DOMAIN": "wiki.test",
    }
    environ.update(extra)
    return environ


def build_portal(tmp_path, *, environ: dict[str, str] | None = None, csrf: bool = False,
                 bot_protection: bool = False, runtime: FakeRuntime | None = None, **overrides: Any):
    cfg = load_config(environ or portal_environ(tmp_path), secret_key="hosting-test-secret-" + "k" * 32,
                      min_form_seconds=0, **overrides)
    app = create_app(cfg, runtime=runtime or FakeRuntime())
    app.config["CSRF_DISABLED"] = not csrf
    with app.app_context(), connection_scope(app.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import settings

        settings.update(bot_protection_enabled=1 if bot_protection else 0)
    return app



def csrf_token(client, path: str = "/login") -> str:
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token on " + path
    return match.group(1)
