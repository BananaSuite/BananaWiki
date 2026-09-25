"""Regenerate the BananaWiki favicons with a chunkier, more emoji-like banana.

Run from the repo root:

    python3 scripts/regen_favicons.py

Renders each colour variant at supersampled resolution, builds a tapered
crescent banana silhouette with body shading, a glossy highlight and a
darker stem + tip, downsamples to 32x32 with Lanczos filtering, and
writes the result to ``app/static/favicons/banana_<colour>.png``.

The shape is drawn as a chain of overlapping circles along the banana's
spine (a quadratic arc), with the circle radius tapering at the ends so
the tips come to a soft point: closer to a Unicode banana emoji than
the symmetric crescent we used to ship.

Self-contained: no JS toolchain, no SVG build step, no dependencies
beyond Pillow (already in ``requirements.txt``).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "favicons"

# Final favicon size.  Browsers crop ICOs to the nearest power of two,
# so 32x32 strikes a good balance between sharpness on retina tabs and
# legibility at 16px scaling in bookmarks bars.
FINAL_SIZE = 32

# Internal supersampling: render at SCALE x FINAL_SIZE, then downsample
# with Lanczos.  8x produced visibly smoother curves than 4x without a
# noticeable size hit.
SCALE = 8
CANVAS = FINAL_SIZE * SCALE

# Each entry maps a colour name to a (body, shadow, gloss, tip) palette.
# - ``body``: the main banana flesh
# - ``shadow``: darker shade for the inner / lower curve
# - ``gloss``: bright streak along the outer / upper curve
# - ``tip``: stem and bottom-tip colour
PALETTE = {
    "yellow": ((255, 213, 67),  (214, 162, 0),  (255, 244, 184), (102, 64, 25)),
    "orange": ((255, 152, 65),  (200, 95, 10),  (255, 220, 170), (102, 50, 15)),
    "red":    ((232, 86, 100),  (164, 30, 50),  (255, 200, 200), (96, 18, 24)),
    "purple": ((171, 113, 224), (108, 60, 168), (235, 210, 255), (60, 30, 96)),
    "blue":   ((92, 152, 240),  (40, 90, 184),  (210, 230, 255), (24, 40, 96)),
    "cyan":   ((82, 205, 222),  (28, 138, 158), (210, 246, 252), (18, 70, 84)),
    "green":  ((110, 196, 110), (45, 130, 60),  (210, 248, 210), (24, 70, 36)),
    "lime":   ((192, 224, 86),  (132, 168, 38), (240, 252, 200), (60, 88, 18)),
}


def _spine_points(size: int, samples: int = 200):
    """Yield ``(x, y, radius)`` for a tapered banana spine.

    The spine follows a quadratic Bezier curve from the upper-right
    (stem) to the lower-left (tip).  Radius peaks in the middle and
    tapers towards both ends.
    """
    # Bezier control points (in canvas-relative 0..1 coordinates).
    # The control point bows the curve outward so the silhouette looks
    # like a banana, not a straight tube.
    p0 = (0.78, 0.18)   # stem end
    p1 = (0.92, 0.92)   # control point pulling the curve out
    p2 = (0.18, 0.82)   # tip end

    # Peak radius (in canvas-relative units) and how strongly the
    # radius tapers at the ends.  ``taper`` close to 0 gives a thick
    # almost-uniform tube; close to 1 gives a sharp lens.
    r_peak = 0.155
    taper = 0.85

    for i in range(samples):
        t = i / (samples - 1)
        # Quadratic Bezier
        omt = 1.0 - t
        x = omt * omt * p0[0] + 2 * omt * t * p1[0] + t * t * p2[0]
        y = omt * omt * p0[1] + 2 * omt * t * p1[1] + t * t * p2[1]
        # Symmetric taper:  full radius at t=0.5, tapered at the ends.
        # ``sin(pi*t)`` gives a smooth 0 -> 1 -> 0 envelope.
        envelope = math.sin(math.pi * t)
        r = r_peak * ((1 - taper) + taper * envelope)
        yield x * size, y * size, r * size


def _body_mask(size: int) -> Image.Image:
    """Build an 8-bit alpha mask for the tapered banana silhouette."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    for x, y, r in _spine_points(size):
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    # Very slight blur to clean up the union of circles.  Without this
    # you can see faint scalloping along the silhouette at 32px.
    mask = mask.filter(ImageFilter.GaussianBlur(radius=size * 0.004))
    # Threshold back to a binary mask so the downstream alpha ops don't
    # bleed colour past the edge.
    mask = mask.point(lambda v: 255 if v >= 128 else 0)
    return mask


def _apply_mask(layer: Image.Image, mask: Image.Image) -> Image.Image:
    """Intersect ``layer``'s alpha channel with ``mask`` in place."""
    layer.putalpha(ImageChops.multiply(layer.split()[3], mask))
    return layer


def render_banana(body, shadow, gloss, tip) -> Image.Image:
    """Render one banana favicon at ``CANVAS x CANVAS`` and return it."""
    size = CANVAS
    mask = _body_mask(size)

    # 1) Filled body
    body_layer = Image.new("RGBA", (size, size), body + (255,))
    body_layer.putalpha(mask)

    # 2) Inner shadow
    # Vertical gradient that fades from transparent at the top to a
    # darker tint at the bottom: gives a sense of light from above.
    shadow_solid = Image.new("RGBA", (size, size), shadow + (255,))
    grad_strip = Image.new("L", (1, size))
    g_px = grad_strip.load()
    for y in range(size):
        g_px[0, y] = int(min(255, max(0, (y / size) * 220)))
    grad = grad_strip.resize((size, size), Image.BILINEAR)
    shadow_solid.putalpha(grad)
    shadow_solid = _apply_mask(shadow_solid, mask)

    # 3) Gloss highlight
    # Draw a second, thinner banana inset along the upper-outer edge
    # and blur it for a soft emoji-style highlight.
    gloss_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss_layer)
    for x, y, r in _spine_points(size):
        # Shift the gloss up-and-out and shrink it to a thin stripe.
        gx = x - size * 0.04
        gy = y - size * 0.06
        gr = r * 0.42
        gd.ellipse(
            (gx - gr, gy - gr, gx + gr, gy + gr),
            fill=gloss + (210,),
        )
    gloss_layer = _apply_mask(gloss_layer, mask)
    gloss_layer = gloss_layer.filter(ImageFilter.GaussianBlur(radius=size * 0.018))

    # 4) Stem (top-right) and tip (bottom-left)
    # Tap the first / last few spine samples to anchor a darker cap
    # that matches the silhouette.
    tip_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tip_layer)
    points = list(_spine_points(size))

    # Top-right stem: a slightly elongated dark blob beyond the first
    # body sample, plus a small overlap into the body for continuity.
    stem_anchor = points[6]   # ~3% from the start
    sx, sy, sr = stem_anchor
    # Direction from this anchor to the *next* anchor: extend the
    # stem along the spine instead of in a fixed orientation.
    next_x, next_y, _ = points[12]
    dx, dy = sx - next_x, sy - next_y
    dlen = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dlen, dy / dlen
    stem_len = size * 0.10
    stem_r = sr * 0.55
    for i in range(8):
        f = i / 7.0
        cx = sx + ux * stem_len * f
        cy = sy + uy * stem_len * f
        td.ellipse(
            (cx - stem_r, cy - stem_r, cx + stem_r, cy + stem_r),
            fill=tip + (255,),
        )

    # Bottom-left tip: a smaller dark cap at the other end.
    end_anchor = points[-6]
    ex, ey, er = end_anchor
    prev_x, prev_y, _ = points[-14]
    dx, dy = ex - prev_x, ey - prev_y
    dlen = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dlen, dy / dlen
    tip_len = size * 0.06
    tip_r = er * 0.50
    for i in range(6):
        f = i / 5.0
        cx = ex + ux * tip_len * f
        cy = ey + uy * tip_len * f
        td.ellipse(
            (cx - tip_r, cy - tip_r, cx + tip_r, cy + tip_r),
            fill=tip + (255,),
        )

    # Mask the tips against the body so they don't draw on transparent
    # pixels (this also blends them softly with the body edge).
    tip_layer = _apply_mask(tip_layer, mask)

    # 5) Dark outer rim
    # An interior outline gives a clean emoji-style edge against light
    # browser themes.  Build it as a ring: full mask minus an eroded
    # copy of the mask.
    eroded = mask.filter(ImageFilter.MinFilter(size=max(3, int(SCALE * 1.1) | 1)))
    rim_mask = ImageChops.subtract(mask, eroded)
    rim_layer = Image.new("RGBA", (size, size), tip + (200,))
    rim_layer.putalpha(rim_mask)

    # Composite the layers
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img = Image.alpha_composite(img, body_layer)
    img = Image.alpha_composite(img, shadow_solid)
    img = Image.alpha_composite(img, gloss_layer)
    img = Image.alpha_composite(img, rim_layer)
    img = Image.alpha_composite(img, tip_layer)
    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for colour, palette in PALETTE.items():
        big = render_banana(*palette)
        small = big.resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)
        out_path = OUT_DIR / f"banana_{colour}.png"
        small.save(out_path, format="PNG", optimize=True)
        print(f"wrote {out_path} ({os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    main()
