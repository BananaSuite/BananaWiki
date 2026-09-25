"""
BananaWiki: Banana favicon generation utilities.

Provides a single helper that produces a 32×32 RGBA banana icon as a Pillow
``Image`` for a given colour preset.  Uses a supersampled quadratic Bézier
spine with a dark rim, inner shadow, and glossy highlight: closer to a
Unicode banana emoji than the previous symmetric crescent shape.

Used both at build time (to ship the default set of favicons) and at runtime
(the "Restore Default Icon" admin action).
"""

from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# ── colour palettes keyed by preset name
# Each value is a (body, shadow, gloss, tip) 4-tuple of RGBA palette entries.
# These are the hand-tuned values from regen_favicons.py.

_FAVICON_PALETTE: dict[str, tuple[tuple[int, int, int], ...]] = {
    "yellow": ((255, 213, 67),   (214, 162, 0),   (255, 244, 184), (102, 64, 25)),
    "orange": ((255, 152, 65),   (200, 95, 10),   (255, 220, 170), (102, 50, 15)),
    "red":    ((232, 86, 100),   (164, 30, 50),   (255, 200, 200), (96, 18, 24)),
    "purple": ((171, 113, 224),  (108, 60, 168),  (235, 210, 255), (60, 30, 96)),
    "blue":   ((92, 152, 240),   (40, 90, 184),   (210, 230, 255), (24, 40, 96)),
    "cyan":   ((82, 205, 222),   (28, 138, 158),  (210, 246, 252), (18, 70, 84)),
    "green":  ((110, 196, 110),  (45, 130, 60),   (210, 248, 210), (24, 70, 36)),
    "lime":   ((192, 224, 86),   (132, 168, 38),  (240, 252, 200), (60, 88, 18)),
}

# Expose a simple name → body-colour mapping for validation & iteration.
# The public type annotation intentionally omits inner tuple structure.
BANANA_FAVICON_COLORS: dict[str, tuple[int, int, int]] = {
    name: palette[0] for name, palette in _FAVICON_PALETTE.items()
}

# ── Rendering constants

FINAL_SIZE = 32
SCALE = 8
CANVAS = FINAL_SIZE * SCALE


# ── Banana geometry

def _spine_points(size: int, samples: int = 200):
    """Yield (x, y, radius) for a tapered banana spine."""
    p0 = (0.78, 0.18)
    p1 = (0.92, 0.92)
    p2 = (0.18, 0.82)

    r_peak = 0.155
    taper = 0.85

    for i in range(samples):
        t = i / (samples - 1)
        omt = 1.0 - t
        x = omt * omt * p0[0] + 2 * omt * t * p1[0] + t * t * p2[0]
        y = omt * omt * p0[1] + 2 * omt * t * p1[1] + t * t * p2[1]
        envelope = math.sin(math.pi * t)
        r = r_peak * ((1 - taper) + taper * envelope)
        yield x * size, y * size, r * size


def _body_mask(size: int) -> Image.Image:
    """Build an 8-bit alpha mask for the tapered banana silhouette."""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    for x, y, r in _spine_points(size):
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=size * 0.004))
    mask = mask.point(lambda v: 255 if v >= 128 else 0)
    return mask


def _apply_mask(layer: Image.Image, mask: Image.Image) -> Image.Image:
    """Intersect layer's alpha channel with mask in place."""
    layer.putalpha(ImageChops.multiply(layer.split()[3], mask))
    return layer


def _render_banana(body, shadow, gloss, tip) -> Image.Image:
    """Render one banana favicon at CANVAS x CANVAS and return it."""
    size = CANVAS
    mask = _body_mask(size)

    # 1) Filled body
    body_layer = Image.new("RGBA", (size, size), body + (255,))
    body_layer.putalpha(mask)

    # 2) Inner shadow (vertical gradient, darker at bottom)
    shadow_solid = Image.new("RGBA", (size, size), shadow + (255,))
    grad_strip = Image.new("L", (1, size))
    g_px = grad_strip.load()
    for y in range(size):
        g_px[0, y] = int(min(255, max(0, (y / size) * 220)))
    grad = grad_strip.resize((size, size), Image.BILINEAR)
    shadow_solid.putalpha(grad)
    shadow_solid = _apply_mask(shadow_solid, mask)

    # 3) Gloss highlight (thin stripe along upper-outer edge)
    gloss_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss_layer)
    for x, y, r in _spine_points(size):
        gx = x - size * 0.04
        gy = y - size * 0.06
        gr = r * 0.42
        gd.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=gloss + (210,))
    gloss_layer = _apply_mask(gloss_layer, mask)
    gloss_layer = gloss_layer.filter(ImageFilter.GaussianBlur(radius=size * 0.018))

    # 4) Stem (top-right) and tip (bottom-left)
    tip_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(tip_layer)
    points = list(_spine_points(size))

    stem_anchor = points[6]
    sx, sy, sr = stem_anchor
    next_x, next_y, _ = points[12]
    dx, dy = sx - next_x, sy - next_y
    dlen = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dlen, dy / dlen
    stem_r = sr * 0.55
    stem_len = size * 0.10
    for i in range(8):
        f = i / 7.0
        cx = sx + ux * stem_len * f
        cy = sy + uy * stem_len * f
        td.ellipse((cx - stem_r, cy - stem_r, cx + stem_r, cy + stem_r), fill=tip + (255,))

    end_anchor = points[-6]
    ex, ey, er = end_anchor
    prev_x, prev_y, _ = points[-14]
    dx, dy = ex - prev_x, ey - prev_y
    dlen = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dlen, dy / dlen
    tip_r = er * 0.50
    tip_len = size * 0.06
    for i in range(6):
        f = i / 5.0
        cx = ex + ux * tip_len * f
        cy = ey + uy * tip_len * f
        td.ellipse((cx - tip_r, cy - tip_r, cx + tip_r, cy + tip_r), fill=tip + (255,))

    tip_layer = _apply_mask(tip_layer, mask)

    # 5) Dark outer rim
    eroded = mask.filter(ImageFilter.MinFilter(size=max(3, int(SCALE * 1.1) | 1)))
    rim_mask = ImageChops.subtract(mask, eroded)
    rim_layer = Image.new("RGBA", (size, size), tip + (200,))
    rim_layer.putalpha(rim_mask)

    # Composite
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img = Image.alpha_composite(img, body_layer)
    img = Image.alpha_composite(img, shadow_solid)
    img = Image.alpha_composite(img, gloss_layer)
    img = Image.alpha_composite(img, rim_layer)
    img = Image.alpha_composite(img, tip_layer)
    return img


# ── Public API

def generate_banana_favicon(preset: str) -> Image.Image:
    """Return a 32×32 RGBA ``Image`` for the named colour *preset*.

    Raises ``ValueError`` if *preset* is not a recognised colour name.
    """
    palette = _FAVICON_PALETTE.get(preset)
    if palette is None:
        raise ValueError(f"Unknown banana favicon preset: {preset!r}")
    big = _render_banana(*palette)
    return big.resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)
