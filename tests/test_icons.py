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

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from bananawiki.core import mascot as sprite
from bananawiki.wiki.templating import FAVICON_PRESETS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_icons.py"
FAVICONS = ROOT / "bananawiki" / "wiki" / "static" / "favicons"
TOUCH = FAVICONS / "apple-touch-icon.png"
HOSTING = ROOT / "bananawiki" / "hosting" / "static"
DESKTOP_ICON = ROOT / "bananawiki" / "desktop" / "static" / "icon.png"
PACKAGING = ROOT / "packaging" / "desktop"
ICO_SIZES = {16, 32, 48, 64, 128, 256}
ICNS_SIZES = {(16, 16, 1), (16, 16, 2), (32, 32, 1), (32, 32, 2), (128, 128, 1), (128, 128, 2),
              (256, 256, 1), (256, 256, 2), (512, 512, 1), (512, 512, 2)}


def make_icons():
    spec = importlib.util.spec_from_file_location("make_icons", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rgb(hex_color: str) -> tuple[int, int, int, int]:
    return (*(int(hex_color[i:i + 2], 16) for i in (1, 3, 5)), 255)


def mascot(light: str = sprite.COLORS["Y"], shade: str = sprite.COLORS["y"]) -> dict[tuple[int, int], tuple]:
    """The sprite's normal face, {(row, column): RGBA}, straight from the mascot's module."""
    colours = {**sprite.COLORS, "Y": light, "y": shade}
    pixels = {(row, column): _rgb(colours[char])
              for row, line in enumerate(sprite.BODY) for column, char in enumerate(line) if char != "."}
    pixels.update({point: _rgb(sprite.COLORS["K"]) for point in (*sprite.EYES, *sprite.MOUTH)})
    return pixels


def grid(image: Image.Image, scale: int, origin: tuple[int, int] = (0, 0)) -> dict[tuple[int, int], tuple]:
    """The 16x16 pixel art in *image*, drawn at *scale* from *origin*; every block must be one colour."""
    image = image.convert("RGBA")
    pixels = {}
    for row in range(sprite.SIZE):
        for column in range(sprite.SIZE):
            left, top = origin[0] + column * scale, origin[1] + row * scale
            block = image.crop((left, top, left + scale, top + scale))
            colours = {colour for _count, colour in block.getcolors(scale * scale)}
            assert len(colours) == 1, f"sprite pixel {row},{column} is not one square of colour: {colours}"
            colour = colours.pop()
            if colour[3]:
                pixels[(row, column)] = colour
    return pixels


def _shift() -> int:
    columns = [column for line in sprite.BODY for column, char in enumerate(line) if char != "."]
    return (sprite.SIZE - (max(columns) - min(columns) + 1)) // 2 - min(columns)


def centred(pixels: dict[tuple[int, int], tuple]) -> dict[tuple[int, int], tuple]:
    """*pixels* moved so the banana sits in the middle of the 16 columns (the sprite leaves two empty on
    the left for the top bar; an icon centres it)."""
    return {(row, column + _shift()): colour for (row, column), colour in pixels.items()}


def test_the_committed_icons_are_the_ones_the_script_draws():
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", "--wiki-only"], capture_output=True, text=True,
                            cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_yellow_favicon_is_the_mascot_with_its_own_colours_and_no_sunglasses():
    image = Image.open(FAVICONS / "banana_yellow.png")
    assert (image.format, image.size, image.has_transparency_data) == ("PNG", (32, 32), True)
    drawn = grid(image, 2)
    assert drawn == centred(mascot())
    # Where the sunglasses would cover the face, the banana shows; no glint either.
    covered = centred({(row, column): None for row, column in set(sprite.SHADES) - set(sprite.EYES)
                       if sprite.BODY[row][column] in "Yy"})
    assert len(covered) >= 6
    assert all(drawn[point] != _rgb(sprite.COLORS["K"]) for point in covered)
    assert _rgb(sprite.COLORS["W"]) not in drawn.values()


@pytest.mark.parametrize("preset", FAVICON_PRESETS)
def test_every_favicon_preset_is_the_mascot_in_its_colour(preset):
    image = Image.open(FAVICONS / f"banana_{preset}.png")
    assert (image.format, image.size, image.has_transparency_data) == ("PNG", (32, 32), True)
    drawn = grid(image, 2)
    light, shade = make_icons().preset_colours(preset)
    assert drawn == centred(mascot(light, shade))
    # Outline, face and stem stay the mascot's; only the two body colours change.
    assert {colour for colour in drawn.values()} == {_rgb(sprite.COLORS[key]) for key in "KB"} | {_rgb(light),
                                                                                                   _rgb(shade)}


def test_the_presets_are_eight_different_colours():
    tool = make_icons()
    pairs = [tool.preset_colours(preset) for preset in FAVICON_PRESETS]
    assert len(set(pairs)) == len(FAVICON_PRESETS) == 8
    assert tool.preset_colours("yellow") == (sprite.COLORS["Y"], sprite.COLORS["y"])
    assert set(tool.PRESETS) == set(FAVICON_PRESETS)


def test_the_portal_uses_the_wiki_s_yellow_icons_byte_for_byte():
    assert (HOSTING / "favicon.png").read_bytes() == (FAVICONS / "banana_yellow.png").read_bytes()
    assert (HOSTING / "apple-touch-icon.png").read_bytes() == TOUCH.read_bytes()


def test_the_home_screen_icon_is_the_banana_on_a_transparent_square():
    """The banana at ten times its size, so half that size (the READMEs) is still exact, with nothing around it:
    iOS fills the transparency with black (the suite's dark background) and rounds the corners itself."""
    image = Image.open(TOUCH).convert("RGBA")
    assert image.size == (180, 180)
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)
    tool = make_icons()
    left, top = tool.touch_origin()
    assert left % 2 == 0 and top % 2 == 0
    assert grid(image, 10, (left, top)) == centred(mascot())
    assert image.getbbox() == tuple(edge * 10 + offset for edge, offset in zip(
        tool.banana().getbbox(), (left, top, left, top), strict=True))


def test_the_home_screen_banana_is_centred_by_eye_not_by_its_box():
    """The stem and the tip stick out to the top right, so a centred bounding box looks low and to the left:
    the banana sits a little higher and further right than that, and stays inside the icon."""
    left, top, right, bottom = Image.open(TOUCH).convert("RGBA").getbbox()
    assert 0 < 180 - right < left and 0 < top < 180 - bottom


def test_the_desktop_window_icon_is_256_pixels():
    image = Image.open(DESKTOP_ICON)
    assert (image.format, image.size, image.has_transparency_data) == ("PNG", (256, 256), True)
    assert grid(image, 16) == centred(mascot())


class _FakePhoto:
    """Tk's PhotoImage as the launcher uses it: subsample keeps every nth pixel from the first, zoom repeats."""

    def __init__(self, file: str | None = None, image: Image.Image | None = None) -> None:
        self.image = Image.open(file).convert("RGBA") if file else image

    def width(self) -> int:
        return self.image.width

    def subsample(self, step: int) -> _FakePhoto:
        pixels = self.image.load()
        size = self.image.width // step
        small = Image.new("RGBA", (size, size))
        small.putdata([pixels[column * step, row * step] for row in range(size) for column in range(size)])
        return _FakePhoto(image=small)

    def zoom(self, factor: int) -> _FakePhoto:
        return _FakePhoto(image=self.image.resize((self.image.width * factor,) * 2, Image.Resampling.NEAREST))


def test_the_desktop_window_gets_the_banana_at_exact_sizes():
    """Window managers given only the 256px image scale it themselves (256 to 24 is 10.67:1) and blur the pixel
    art, so the launcher offers whole multiples of the sprite as well."""
    from bananawiki.desktop.app import window_icons

    images = window_icons(_FakePhoto, DESKTOP_ICON)
    assert [image.width() for image in images] == [256, 48, 32, 16]
    for image in images:
        assert grid(image.image, image.width() // 16) == centred(mascot()), image.width()


def test_the_windows_icon_has_every_whole_multiple_of_the_sprite():
    image = Image.open(PACKAGING / "bananawiki.ico")
    assert image.format == "ICO" and set(image.info["sizes"]) == {(size, size) for size in ICO_SIZES}
    for size in ICO_SIZES:
        frame = image.ico.getimage((size, size))
        assert frame.size == (size, size)
        assert grid(frame, size // 16) == centred(mascot()), size


def test_the_macos_icon_has_every_size_from_16_to_1024():
    image = Image.open(PACKAGING / "bananawiki.icns")
    assert image.format == "ICNS" and set(image.info["sizes"]) == ICNS_SIZES
    for width, height, scale in ICNS_SIZES:
        frame = image.icns.getimage((width, height, scale))
        assert frame.size == (width * scale, height * scale)
        assert grid(frame, width * scale // 16) == centred(mascot()), (width, scale)


def test_the_desktop_build_uses_the_generated_icons():
    spec = (PACKAGING / "bananawiki-desktop.spec").read_text(encoding="utf-8")
    assert spec.count('HERE / "bananawiki.icns"') == 2 and 'HERE / "bananawiki.ico"' in spec
    assert 'Path(__file__).resolve().parent / "static" / "icon.png"' in (
        ROOT / "bananawiki" / "desktop" / "app.py").read_text(encoding="utf-8")


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


def test_metadata_in_an_icon_counts_as_a_difference(tmp_path):
    """Same pixels with a text chunk (an image editor's, a timestamp) are not what the script writes."""
    tool, root = make_icons(), _tree(tmp_path)
    touched = root / "bananawiki" / "wiki" / "static" / "favicons" / "banana_blue.png"
    info = PngInfo()
    info.add_text("Software", "an image editor")
    Image.open(touched).save(touched, pnginfo=info)
    assert tool.sync(tool.targets(root, None, None), write=False) == [touched]


def test_reruns_write_the_same_bytes(tmp_path):
    # Each run loads the script afresh: drawn() and sprite() are cached, so a second sync by the same module
    # would write the first run's bytes again and hide any nondeterminism.
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        tool = make_icons()
        tool.sync(tool.targets(root, root / "chat", root / "site"), write=True)
    files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    assert len(files) == 8 + 1 + 2 + 1 + 2 + 2 + 2
    assert all((first / name).read_bytes() == (second / name).read_bytes() for name in files)
    for name in files:
        data = (first / name).read_bytes()
        assert b"tIME" not in data and b"tEXt" not in data and b"iTXt" not in data and b"pHYs" not in data, name


def test_copies_in_the_other_trees_are_byte_identical_to_this_tree_s(tmp_path):
    tool = make_icons()
    root = _tree(tmp_path / "wiki")
    chat, site = tmp_path / "chat", tmp_path / "site"
    targets = tool.targets(root, chat, site)
    assert len(tool.sync(targets, write=True)) == 4
    yellow = (FAVICONS / "banana_yellow.png").read_bytes()
    assert (chat / "bananachat" / "static" / "img" / "favicon.png").read_bytes() == yellow
    assert (site / "img" / "banana_yellow.png").read_bytes() == yellow
    assert (chat / "bananachat" / "static" / "img" / "apple-touch-icon.png").read_bytes() == TOUCH.read_bytes()
    assert (site / "img" / "apple-touch-icon.png").read_bytes() == TOUCH.read_bytes()


@pytest.mark.parametrize("relative", ["bananachat/static/js/mascot.js", "mascot/mascot.js"])
def test_a_change_to_the_mascot_fails_until_each_port_records_it(tmp_path, relative):
    tool = make_icons()
    assert relative in (tool.CHAT_PORT, tool.SITE_PORT)
    wiki, tree = tmp_path / "wiki", tmp_path / "port"
    for name in tool.MASCOT_SOURCES:
        (wiki / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, wiki / name)
    port = tree / relative
    port.parent.mkdir(parents=True)
    port.write_text(f"// Ported from BananaWiki mascot {tool.mascot_version(wiki)} (...).\nimport x;\n", encoding="utf-8")
    assert tool.port(wiki, tree, relative, record=False) is None
    script = wiki / "bananawiki" / "core" / "static" / "mascot" / "mascot.js"
    script.write_text(script.read_text(encoding="utf-8") + "// a fix\n", encoding="utf-8")
    assert "--ported" in tool.port(wiki, tree, relative, record=False)
    assert tool.port(wiki, tree, relative, record=True) is None
    assert port.read_text(encoding="utf-8") == (
        f"// Ported from BananaWiki mascot {tool.mascot_version(wiki)} (...).\nimport x;\n")
    assert tool.port(wiki, tree, relative, record=False) is None
    port.write_text("import x;\n", encoding="utf-8")
    assert tool.port(wiki, tree, relative, record=True).endswith('no "BananaWiki mascot <hash>" line')


@pytest.mark.parametrize(("sibling", "marker", "relative"), [("BananaChat", "bananachat/static", "CHAT_PORT"),
                                                             ("BananaWiki-site", "img", "SITE_PORT")])
def test_the_sibling_ports_of_the_mascot_follow_this_tree_s(sibling, marker, relative):
    tree = ROOT.parent / sibling
    if not (tree / marker).is_dir():
        pytest.skip(f"{sibling} is not next to this tree")
    tool = make_icons()
    assert tool.port(ROOT, tree, getattr(tool, relative), record=False) is None


def test_the_script_reads_the_sprite_without_flask_or_markupsafe(tmp_path):
    probe = (
        "import builtins, runpy, sys\n"
        "real = builtins.__import__\n"
        "def guard(name, *args, **kwargs):\n"
        "    if name.split('.')[0] in ('flask', 'markupsafe', 'jinja2', 'bananawiki'):\n"
        "        raise ImportError(name)\n"
        "    return real(name, *args, **kwargs)\n"
        "builtins.__import__ = guard\n"
        "sys.argv = ['make_icons.py', '--check', '--wiki-only']\n"
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pngs_are_whole_squares_when_browsers_halve_them():
    """A browser shows a 32x32 favicon at 16x16 on a standard screen: every 2x2 block is one colour, so the
    halved icon is the sprite again, not a blur."""
    image = Image.open(FAVICONS / "banana_yellow.png").convert("RGBA")
    halved = image.reduce(2)
    assert halved.size == (16, 16) and grid(halved, 1) == centred(mascot())
