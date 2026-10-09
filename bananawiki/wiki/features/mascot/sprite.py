"""The mascot's pixel art, drawn as inline SVG.

The banana is a 16x16 grid. Each layer is a set of pixels; the SVG merges
horizontal runs of one colour into a single ``<rect>``. The face, the sunglasses
and the eyes are separate groups so CSS can blink the eyes and drop the glasses
on without redrawing anything.
"""

from __future__ import annotations

from functools import cache

from markupsafe import Markup

SIZE = 16

# K outline, Y light yellow, y shaded yellow, B stem tip.
BODY = (
    ".........KKK....",
    ".........KBK....",
    ".....KKKKKK.....",
    "....KyYYYK......",
    "...KyYYYYK......",
    "...KyYYYYK......",
    "..KyyYYYYYK.....",
    "..KyyYYYYYK.....",
    "..KyyYYYYYYKK...",
    "..KyyYYYYYYYYKK.",
    "..KyyyYYYYYYYYYK",
    "...KyyyYYYYYYYyK",
    "...KyyyyyyYYYyK.",
    "....KKyyyyyyyK..",
    "......KKKKKKK...",
    "................",
)
EYES = ((6, 5), (7, 5), (6, 8), (7, 8))
MOUTH = ((10, 5), (11, 6), (11, 7), (10, 8))
SHADES = tuple((6, column) for column in range(2, 11) if column != 4) + ((7, 4), (7, 5), (7, 7), (7, 8))
GLINT = ((6, 4),)

COLORS = {"K": "#1a1408", "Y": "#ffde4a", "y": "#e2ba28", "B": "#6e461e", "W": "#ffffff"}
VARIANTS = ("normal", "shades", "off")


def _runs(pixels: dict[tuple[int, int], str]) -> str:
    """``<rect>`` elements for *pixels* ({(row, column): colour}), one per horizontal run."""
    rects = []
    for row in range(SIZE):
        column = 0
        while column < SIZE:
            color = pixels.get((row, column))
            if color is None:
                column += 1
                continue
            start = column
            while pixels.get((row, column)) == color:
                column += 1
            rects.append(f'<rect x="{start}" y="{row}" width="{column - start}" height="1" fill="{color}"/>')
    return "".join(rects)


def _body(fill: str | None = None) -> dict[tuple[int, int], str]:
    return {(row, column): fill or COLORS[char]
            for row, line in enumerate(BODY) for column, char in enumerate(line) if char != "."}


def _layer(points: tuple[tuple[int, int], ...], color: str) -> dict[tuple[int, int], str]:
    return {point: color for point in points}


@cache
def svg(variant: str, css_class: str = "") -> Markup:
    """The sprite in one of :data:`VARIANTS`.

    ``normal`` and ``shades`` share the same markup (the sunglasses group is
    shown by the ``mascot-sprite--shades`` class), so a script can switch
    between them. ``off`` is the silhouette in ``currentColor``.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown mascot variant {variant!r}")
    classes = " ".join(filter(None, ("mascot-sprite", "mascot-sprite--shades" if variant == "shades" else "",
                                     css_class)))
    head = (f'<svg class="{classes}" viewBox="0 0 {SIZE} {SIZE}" shape-rendering="crispEdges" '
            'aria-hidden="true" focusable="false">')
    if variant == "off":
        return Markup(head + _runs(_body("currentColor")) + "</svg>")
    dark = COLORS["K"]
    return Markup(
        head + _runs(_body())
        + '<g class="mascot__eyes">' + _runs(_layer(EYES, dark)) + "</g>"
        + _runs(_layer(MOUTH, dark))
        + '<g class="mascot__shades">' + _runs({**_layer(SHADES, dark), **_layer(GLINT, COLORS["W"])}) + "</g>"
        + "</svg>"
    )
