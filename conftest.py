"""Shared fixtures for core and bundled-plugin tests."""
import atexit
import os
import shutil
import tempfile

import pytest

# A few test modules import the application at module level, which initialises
# whatever database DATABASE_PATH names. That happens while pytest is still
# collecting, so without a private file every xdist worker would initialise the
# same database at the same moment. Individual tests still point
# config.DATABASE_PATH at their own tmp_path; this only covers collection.
#
# The worker processes inherit the controller's environment, so a value chosen
# once at import time would be shared rather than private. Record which process
# chose it and choose again when the value was inherited from another one. A
# path the operator set themselves carries no owner and is always respected.
_OWNER = "BW_COLLECTION_DATABASE_PID"
if "BW_DATABASE_PATH" not in os.environ or os.environ.get(_OWNER) not in (None, str(os.getpid())):
    _collection_scratch = tempfile.mkdtemp(prefix="bananawiki-collect-")
    os.environ["BW_DATABASE_PATH"] = os.path.join(_collection_scratch, "collect.db")
    os.environ[_OWNER] = str(os.getpid())
    atexit.register(shutil.rmtree, _collection_scratch, ignore_errors=True)


@pytest.fixture(autouse=True)
def plugin_archive_from_source(request, tmp_path, monkeypatch):
    """Plugin import tests use current source instead of checked-in ZIP output."""
    if not hasattr(request.module, "BWPLUGIN_PATH"):
        return
    import json
    from pathlib import Path
    import zipfile
    source = Path(request.module.__file__).resolve().parents[1]
    archive = tmp_path / (source.name + ".bwplugin")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if not path.is_file() or path.is_symlink() or any(part in {"tests", "__pycache__"} for part in relative.parts):
                continue
            if relative.as_posix() == "plugin.json":
                manifest = json.loads(path.read_text())
                manifest["builtin"] = False
                output.writestr("plugin.json", json.dumps(manifest))
            else:
                output.write(path, relative.as_posix())
    monkeypatch.setattr(request.module, "BWPLUGIN_PATH", str(archive))
