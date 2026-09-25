# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(PROJECT_ROOT))


block_cipher = None

hiddenimports = [
    "app",
    "archive_format",
    "config",
    "plugin_loader",
    "qrcode",
    "tzdata",
    "wiki_logger",
    "wsgi",
]
for package in (
    "db",
    "helpers",
    "routes",
    "plugins",
    "bananawiki_sdk",
):
    hiddenimports.extend(collect_submodules(package))

datas = [
    ("../app/templates", "app/templates"),
    ("../app/static", "app/static"),
    ("../plugins", "plugins"),
    ("../translations", "translations"),
    ("../bananawiki_sdk", "bananawiki_sdk"),
]
datas.append((str(PROJECT_ROOT / "db/migrations/legacy_schema.sql"), "db/migrations"))

# Include tzdata zone info files for Windows timezone support
import importlib.resources
try:
    _tzdata_path = str(importlib.resources.files("tzdata"))
    datas.append((_tzdata_path, "tzdata"))
except Exception:
    pass

a = Analysis(
    ["main.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["hosting", "tests"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="BananaWikiEasyDeployment",
        icon="banana_icon.icns",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=True,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="BananaWikiEasyDeployment",
    )
    app = BUNDLE(
        coll,
        name="BananaWiki Easy Deployment.app",
        icon="banana_icon.icns",
        bundle_identifier="it.canalescuola.bananawiki.easydeployment",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        exclude_binaries=False,
        name="BananaWikiEasyDeployment",
        icon="banana_icon.ico",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
