# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for BananaWiki Desktop. Build with: python packaging/desktop/build.py
#
# Windows and Linux: one executable file. macOS: an .app bundle (onedir).
# The bundle contains the wiki (bananawiki.core, bananawiki.wiki, bananawiki.sdk)
# and the launcher (bananawiki.desktop), served by waitress. The hosting portal,
# the server lifecycle tools and gunicorn are left out.

import importlib.util
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)
ROOT = HERE.parents[1]
NAME = "BananaWiki"
BUNDLE_ID = "org.bananawiki.desktop"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "bananawiki" / "__init__.py").read_text()).group(1)
EXCLUDED_PACKAGES = ("bananawiki.hosting", "bananawiki.ops")

sys.path.insert(0, str(ROOT))

hiddenimports = collect_submodules("bananawiki", filter=lambda name: not name.startswith(EXCLUDED_PACKAGES))
hiddenimports += collect_submodules("waitress")
datas = collect_data_files("bananawiki", excludes=["hosting/**", "ops/**", "**/__pycache__/**"])
if importlib.util.find_spec("tzdata") is not None:  # zoneinfo data, needed on Windows
    hiddenimports += collect_submodules("tzdata")
    datas += collect_data_files("tzdata")

a = Analysis(
    [str(ROOT / "bananawiki" / "desktop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gunicorn", "pytest", *EXCLUDED_PACKAGES],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        icon=str(HERE / "bananawiki.icns"),
        debug=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(HERE / "bananawiki.icns"),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "LSApplicationCategoryType": "public.app-category.education",
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        icon=str(HERE / "bananawiki.ico"),
        debug=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
    )
