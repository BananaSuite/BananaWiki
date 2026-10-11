#!/usr/bin/env python3
"""Draw every BananaSuite icon from the mascot's pixel art.

The banana of ``bananawiki/wiki/features/mascot/sprite.py`` (its body, eyes
and mouth: the normal face, without the sunglasses) is the icon of the wiki,
the hosting portal, the desktop app, BananaChat and the website. Every sprite
pixel becomes an exact square (whole-number scales, no smoothing), the banana
is centred, and the files carry no metadata, so a rerun writes the same bytes:

    bananawiki/wiki/static/favicons/banana_<colour>.png   favicon presets (Admin > Appearance), 32x32
    bananawiki/wiki/static/favicons/apple-touch-icon.png  home-screen icon, 180x180 on a light tile
    bananawiki/hosting/static/favicon.png                 the portal's icon: the yellow preset
    bananawiki/hosting/static/apple-touch-icon.png
    bananawiki/desktop/static/icon.png                    the desktop app's window icon, 256x256
    packaging/desktop/bananawiki.ico                      Windows: 16, 32, 48, 64, 128 and 256 pixels
    packaging/desktop/bananawiki.icns                     macOS: 16 to 1024 pixels

and, when the trees sit next to this repository (or --chat and --site name
them), BananaChat's ``bananachat/static/img/favicon.png`` and
``apple-touch-icon.png`` and the website's ``img/banana_yellow.png`` and
``img/apple-touch-icon.png``: copies of the wiki's yellow favicon and
home-screen icon. Run it after changing the sprite::

    python scripts/make_icons.py              write the icons that differ
    python scripts/make_icons.py --check      list the icons that differ and exit 1, writing nothing
    python scripts/make_icons.py --wiki-only  only this repository's icons (with or without --check)

An icon differs when its pixels do, or when it carries anything besides
pixels (a text chunk, a timestamp). PNG compression varies between zlib
builds, so a file with the right pixels is left as it is, and the copies in
the other trees must be byte-identical to this repository's. The sprite
module is read, not imported: no Flask or markupsafe needed, only Pillow.
"""

from __future__ import annotations

import argparse
import ast
import io
import struct
import sys
from functools import cache
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPRITE = ROOT / "bananawiki" / "wiki" / "features" / "mascot" / "sprite.py"

# Light and shaded body colour (the sprite's Y and y) of each favicon preset. Yellow is the sprite's own
# pair. The others keep the hue of the two main colours of the earlier preset icons (light/shade:
# green #b9e7ba/#45974e, red #efabae/#b93546, blue #b6d0f5/#3b6ec6, purple #d5b8ef/#804eb7,
# orange #f9c991/#d27120, cyan #b5e7f0/#319fb1, lime #e1f1ae/#96b738), with the lightness and saturation
# moved as much as it takes to turn the earlier yellow (#fae89c/#d7b01d) into the sprite's: the colours
# stay recognisable and are as bright as the mascot.
PRESETS = {
    "yellow": None,
    "green": ("#7ad77c", "#4ba555"),
    "red": ("#e96369", "#c73c4e"),
    "blue": ("#68a1f2", "#4d7bcb"),
    "purple": ("#af71e6", "#8c5fbe"),
    "orange": ("#fea741", "#de7c29"),
    "cyan": ("#6dd6e8", "#35adc1"),
    "lime": ("#ccec64", "#a2c540"),
}
FAVICON_SCALE = 2  # 32x32; a standard screen halves it back to the sprite
DESKTOP_SCALE = 16  # 256x256
ICO_SIZES = (16, 32, 48, 64, 128, 256)
# macOS icon types, each a PNG: 16, 32, 128, 256 and 512 points at single and double density.
ICNS_TYPES = ((b"icp4", 16), (b"ic11", 32), (b"icp5", 32), (b"ic12", 64), (b"ic07", 128), (b"ic13", 256),
              (b"ic08", 256), (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024))
# Home screens (iOS shows transparency in black and rounds the corners itself): an opaque light square,
# the banana at ten times its size, so the half-size picture in the READMEs is still exact.
TOUCH_SIZE = 180
TOUCH_SCALE = 10
TOUCH_TILE = "#fffaeb"
PNG_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}


@cache
def sprite(path: Path = SPRITE) -> dict:
    """SIZE, BODY, EYES, MOUTH and COLORS of the sprite module, read from its source without running it."""
    wanted = {"SIZE", "BODY", "EYES", "MOUTH", "COLORS"}
    found = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in wanted):
            found[node.targets[0].id] = ast.literal_eval(node.value)
    if missing := wanted - found.keys():
        raise SystemExit(f"{path}: no literal {', '.join(sorted(missing))}")
    return found


def preset_colours(preset: str) -> tuple[str, str]:
    """The light and shaded body colour of *preset*."""
    return PRESETS[preset] or (sprite()["COLORS"]["Y"], sprite()["COLORS"]["y"])


def _rgba(hex_color: str) -> tuple[int, int, int, int]:
    return (*(int(hex_color[index:index + 2], 16) for index in (1, 3, 5)), 255)


def banana(preset: str = "yellow") -> Image.Image:
    """The sprite's banana with its normal face, 16x16, in *preset*'s body colours, centred across the
    columns (the sprite keeps two empty columns on the left for the top bar)."""
    art = sprite()
    light, shade = preset_colours(preset)
    colours = {**art["COLORS"], "Y": light, "y": shade}
    pixels = {(row, column): colours[char]
              for row, line in enumerate(art["BODY"]) for column, char in enumerate(line) if char != "."}
    pixels.update({point: art["COLORS"]["K"] for point in (*art["EYES"], *art["MOUTH"])})
    columns = [column for _row, column in pixels]
    shift = (art["SIZE"] - (max(columns) - min(columns) + 1)) // 2 - min(columns)
    image = Image.new("RGBA", (art["SIZE"], art["SIZE"]), (0, 0, 0, 0))
    for (row, column), colour in pixels.items():
        image.putpixel((column + shift, row), _rgba(colour))
    return image


def scaled(image: Image.Image, factor: int) -> Image.Image:
    """*image* with every pixel an exact *factor* x *factor* square."""
    return image.resize((image.width * factor, image.height * factor), Image.Resampling.NEAREST)


def touch_origin() -> tuple[int, int]:
    """Where the home-screen icon's 16x16 grid starts: the banana in the middle, on even pixels."""
    box = banana().getbbox()
    width, height = (box[2] - box[0]) * TOUCH_SCALE, (box[3] - box[1]) * TOUCH_SCALE
    left = (TOUCH_SIZE - width) // 4 * 2 - box[0] * TOUCH_SCALE
    top = (TOUCH_SIZE - height) // 4 * 2 - box[1] * TOUCH_SCALE
    return left, top


def touch_icon() -> Image.Image:
    """The home-screen icon: the yellow banana on its light square."""
    image = Image.new("RGBA", (TOUCH_SIZE, TOUCH_SIZE), _rgba(TOUCH_TILE))
    image.alpha_composite(scaled(banana(), TOUCH_SCALE), touch_origin())
    return image


def png(image: Image.Image) -> bytes:
    """*image* as a PNG with nothing but its pixels (no text, time or resolution chunks)."""
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=9)
    return buffer.getvalue()


def ico(images: list[Image.Image]) -> bytes:
    """A Windows icon of PNG frames."""
    frames = [png(image) for image in images]
    offset = 6 + 16 * len(frames)
    head = [struct.pack("<HHH", 0, 1, len(frames))]
    for image, frame in zip(images, frames, strict=True):
        head.append(struct.pack("<BBBBHHII", image.width % 256, image.height % 256, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    return b"".join(head + frames)


def icns(frames: dict[int, bytes]) -> bytes:
    """A macOS icon: one PNG per type of :data:`ICNS_TYPES`, from *frames* ({pixels: PNG})."""
    body = b"".join(kind + struct.pack(">I", 8 + len(frames[size])) + frames[size] for kind, size in ICNS_TYPES)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


@cache
def drawn() -> dict[str, bytes]:
    """Every icon, by name."""
    icons = {f"favicon-{preset}": png(scaled(banana(preset), FAVICON_SCALE)) for preset in PRESETS}
    icons["touch"] = png(touch_icon())
    icons["desktop"] = png(scaled(banana(), DESKTOP_SCALE))
    grid = sprite()["SIZE"]
    icons["ico"] = ico([scaled(banana(), size // grid) for size in ICO_SIZES])
    icons["icns"] = icns({size: png(scaled(banana(), size // grid)) for size in {size for _kind, size in ICNS_TYPES}})
    return icons


def targets(wiki: Path, chat: Path | None, site: Path | None) -> list[tuple[Path, str]]:
    """(file, icon name) for this repository at *wiki* and the BananaChat and website trees, if given.
    The first file of each icon is the one its copies must match."""
    package = wiki / "bananawiki"
    found = [(package / "wiki" / "static" / "favicons" / f"banana_{preset}.png", f"favicon-{preset}")
             for preset in PRESETS]
    found += [
        (package / "wiki" / "static" / "favicons" / "apple-touch-icon.png", "touch"),
        (package / "hosting" / "static" / "favicon.png", "favicon-yellow"),
        (package / "hosting" / "static" / "apple-touch-icon.png", "touch"),
        (package / "desktop" / "static" / "icon.png", "desktop"),
        (wiki / "packaging" / "desktop" / "bananawiki.ico", "ico"),
        (wiki / "packaging" / "desktop" / "bananawiki.icns", "icns"),
    ]
    if chat is not None:
        found += [(chat / "bananachat" / "static" / "img" / "favicon.png", "favicon-yellow"),
                  (chat / "bananachat" / "static" / "img" / "apple-touch-icon.png", "touch")]
    if site is not None:
        found += [(site / "img" / "banana_yellow.png", "favicon-yellow"),
                  (site / "img" / "apple-touch-icon.png", "touch")]
    return found


def _png_frames(data: bytes) -> list[tuple[str, tuple[int, int], bytes]]:
    """[(mode, size, pixels)] of a PNG; ValueError for anything but the chunks the script writes."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    position = 8
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        if kind not in PNG_CHUNKS:
            raise ValueError(f"PNG chunk {kind!r}")
        position += 12 + length
    image = Image.open(io.BytesIO(data))
    image.load()
    return [(image.mode, image.size, image.tobytes())]


def _frames(data: bytes) -> list:
    """What an icon shows: its PNG frames' pixels, with their place in an .ico or .icns."""
    if data[:4] == b"icns":
        frames, position = [], 8
        while position < len(data):
            kind, length = struct.unpack(">4sI", data[position:position + 8])
            if length < 8:
                raise ValueError(f"icns entry {kind!r} of {length} bytes")
            frames.append((kind, _png_frames(data[position + 8:position + length])))
            position += length
        return frames
    if data[:4] == b"\0\0\1\0":
        count = struct.unpack("<H", data[4:6])[0]
        entries = [struct.unpack("<BBBBHHII", data[6 + 16 * index:22 + 16 * index]) for index in range(count)]
        return [(entry[:6], _png_frames(data[entry[7]:entry[7] + entry[6]])) for entry in entries]
    return _png_frames(data)


def same(current: bytes, wanted: bytes) -> bool:
    """Whether *current* shows exactly what *wanted* does and carries nothing else."""
    if current == wanted:
        return True
    try:
        return _frames(current) == _frames(wanted)
    except (ValueError, OSError, struct.error):
        return False


def sync(files: list[tuple[Path, str]], *, write: bool) -> list[Path]:
    """The files of *files* that differ from the drawn icons, rewritten when *write* is true."""
    icons, canonical, differing = drawn(), {}, []
    for path, name in files:
        current = path.read_bytes() if path.is_file() else None
        if name not in canonical:
            keep = current is not None and same(current, icons[name])
            canonical[name] = current if keep else icons[name]
        if current != canonical[name]:
            differing.append(path)
            if write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(canonical[name])
    return differing


def _sibling(given: str | None, default: Path, marker: str) -> Path | None:
    if given is not None:
        path = Path(given)
        if not (path / marker).exists():
            raise SystemExit(f"{path}: no {marker} there")
        return path
    return default if (default / marker).exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="list the icons that differ and exit 1")
    parser.add_argument("--wiki-only", action="store_true", help="only this repository's icons")
    parser.add_argument("--chat", help="the BananaChat tree (default: ../BananaChat, when it is there)")
    parser.add_argument("--site", help="the website tree (default: ../BananaWiki-site, when it is there)")
    args = parser.parse_args(argv)
    chat = site = None
    if not args.wiki_only:
        chat = _sibling(args.chat, ROOT.parent / "BananaChat", "bananachat/static")
        site = _sibling(args.site, ROOT.parent / "BananaWiki-site", "img")
    files = targets(ROOT, chat, site)
    differing = sync(files, write=not args.check)
    for path in differing:
        print(f"{'differs' if args.check else 'wrote'}: {path}")
    trees = ", ".join(str(tree) for tree in (ROOT, chat, site) if tree is not None)
    if args.check and differing:
        print(f"{len(differing)} of {len(files)} icons differ from the sprite: run python scripts/make_icons.py")
        return 1
    if not differing:
        print(f"All {len(files)} icons match the sprite ({trees}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
