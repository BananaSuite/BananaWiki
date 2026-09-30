"""Helpers for the plugin tests: plugin folders, archives and extra features."""

from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from pathlib import Path

from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.registry import Feature

REPO = Path(__file__).resolve().parents[2]
HELLO_PLUGIN = REPO / "bananawiki" / "wiki" / "features" / "plugin_manager" / "examples" / "hello_plugin"
LEGACY_EXAMPLES = REPO / "docs" / "plugins" / "examples"


def plugins_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plugins"
    path.mkdir(exist_ok=True)
    return path


def install_folder(tmp_path: Path, source: Path, name: str | None = None) -> Path:
    target = plugins_dir(tmp_path) / (name or source.name)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def write_plugin(tmp_path: Path, plugin_id: str, init: str, *, schema: str | None = None,
                 manifest: dict | None = None) -> Path:
    folder = plugins_dir(tmp_path) / plugin_id
    folder.mkdir()
    data = {"id": plugin_id, "name": plugin_id.title(), "version": "1.0.0"}
    data.update(manifest or {})
    (folder / "plugin.json").write_text(json.dumps(data))
    (folder / "__init__.py").write_text(init)
    if schema is not None:
        (folder / "schema.py").write_text(schema)
    return folder


def build(app_factory, tmp_path: Path, **environ: str):
    env = {"BW_EXTERNAL_PLUGINS_DIR": str(plugins_dir(tmp_path)), **environ}
    return app_factory(environ=env)


def set_row(app, plugin_id: str, enabled: bool) -> None:
    with app.app_context(), connection_scope() as session:
        session.execute("UPDATE plugins SET enabled = ? WHERE id = ?", (1 if enabled else 0, plugin_id))


def row(app, plugin_id: str):
    with app.app_context(), connection_scope() as session:
        return session.one("SELECT * FROM plugins WHERE id = ?", (plugin_id,))


def start_enabled(app_factory, tmp_path: Path, plugin_id: str, **environ: str):
    """Start once (registers the folder disabled), enable the row, start again."""
    first = build(app_factory, tmp_path, **environ)
    set_row(first, plugin_id, True)
    return build(app_factory, tmp_path, **environ)


def add_feature(app, feature_id: str, **options) -> Feature:
    """Register a switchable test feature after start-up."""
    feature = Feature(id=feature_id, name=f"test.{feature_id}", toggle=options.pop("toggle", "plugin"), **options)
    app.extensions["bananawiki.registry"].add(feature)
    with app.app_context(), connection_scope() as session:
        session.execute(
            "INSERT OR IGNORE INTO plugins (id, name, version, builtin, enabled) VALUES (?, ?, '1.6.0', 1, ?)",
            (feature_id, feature_id, 1 if feature.default_enabled else 0),
        )
    return feature


def archive(files: dict[str, bytes | str], *, links: tuple[str, ...] = (), compress: bool = True) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", method) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def plugin_files(plugin_id: str = "uploaded", **manifest) -> dict[str, str]:
    data = {"id": plugin_id, "name": "Uploaded", "version": "1.0.0", **manifest}
    return {
        "plugin.json": json.dumps(data),
        "__init__.py": "from bananawiki.wiki.registry import Feature\n"
                       f"FEATURE = Feature(id={plugin_id!r}, name='x')\n",
    }


def upload(client, content: bytes, *, password: str, filename: str = "plugin.bwplugin"):
    return client.post("/admin/plugins/import", data={
        "plugin_file": (io.BytesIO(content), filename), "password": password,
    }, content_type="multipart/form-data")
