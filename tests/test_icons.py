"""Every icon is the mascot's banana, drawn by scripts/make_icons.py.

The favicon presets of Admin > Appearance, the logo, the hosting portal's
icon, the home-screen icons and the desktop app's icons (.png for its window,
.ico for Windows, .icns for macOS) come from the pixel art in
bananawiki/core/mascot.py: its body, eyes and mouth, without
the sunglasses, each sprite pixel an exact square. ``make_icons.py --check``
fails when an icon was edited by hand or the sprite changed without a rerun.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_icons.py"
FAVICONS = ROOT / "bananawiki" / "wiki" / "static" / "favicons"
def make_icons():
    spec = importlib.util.spec_from_file_location("make_icons", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_icons_are_the_ones_the_script_draws():
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", "--wiki-only"], capture_output=True, text=True,
                            cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def _tree(tmp_path: Path) -> Path:
    """A copy of the files the script reads and writes."""
    tool = make_icons()
    for path, _name in tool.targets(ROOT, None, None):
        copy = tmp_path / path.relative_to(ROOT)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copy)
    return tmp_path


def test_check_names_an_icon_changed_by_hand_and_a_rerun_restores_it(tmp_path):
    tool, root = make_icons(), _tree(tmp_path)
    edited = root / "bananawiki" / "wiki" / "static" / "favicons" / "banana_red.png"
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(edited)
    missing = root / "packaging" / "desktop" / "bananawiki.icns"
    missing.unlink()
    targets = tool.targets(root, None, None)
    assert tool.sync(targets, write=False) == [edited, missing]
    assert not missing.exists()
    assert tool.sync(targets, write=True) == [edited, missing]
    assert tool.sync(targets, write=False) == []
    assert tool.same(edited.read_bytes(), (FAVICONS / "banana_red.png").read_bytes())
