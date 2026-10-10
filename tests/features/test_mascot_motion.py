"""Mascot motion and touch: still for whoever asks for less motion, a finger-sized target on touch screens.

The stylesheets are read as text: a small parser splits them into rules, so each
test checks the declaration a browser would apply, not a substring.
"""

from __future__ import annotations

import re
from pathlib import Path

from bananawiki.core import assets
from bananawiki.wiki.features import mascot

SHELL_CSS = assets.SHARED_STATIC_ROOT / "css" / "bananawiki.css"
MASCOT_CSS = Path(mascot.__file__).parent / "static" / "mascot.css"
REDUCED = "@media (prefers-reduced-motion: reduce)"
TILT = "rotate(-8deg)"


def _css(path: Path) -> str:
    """The stylesheet without comments (a checkout may have CRLF line endings)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _rules(css: str) -> list[tuple[str, str]]:
    """Top-level ``(prelude, body)`` pairs; an at-rule's body is the CSS nested in it."""
    rules, depth, start, prelude = [], 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                prelude, start = " ".join(css[start:index].split()), index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                rules.append((prelude, css[start:index]))
                start = index + 1
    return rules


def _style_rules(css: str, skip: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Every style rule, at-rules opened, except keyframes and the at-rules in *skip*."""
    found = []
    for prelude, body in _rules(css):
        if prelude in skip or prelude.startswith("@keyframes"):
            continue
        found += _style_rules(body, skip) if prelude.startswith("@") else [(prelude, body)]
    return found


def _media(css: str, query: str) -> list[tuple[str, str]]:
    """The rules inside every ``@media (query)`` block."""
    return [rule for prelude, body in _rules(css) if prelude == f"@media ({query})" for rule in _rules(body)]


def _top(css: str) -> list[tuple[str, str]]:
    """The style rules outside any at-rule."""
    return [rule for rule in _rules(css) if not rule[0].startswith("@")]


def _selectors(prelude: str) -> set[str]:
    return {" ".join(part.split()) for part in prelude.split(",")}


def _declarations(body: str) -> dict[str, str]:
    pairs = (part.split(":", 1) for part in body.split(";") if ":" in part)
    return {name.strip(): " ".join(value.split()) for name, value in pairs}


def _declared(rules: list[tuple[str, str]], selector: str, name: str) -> str | None:
    """The value *rules* give to *name* for exactly *selector* (the last one wins), or None."""
    value = None
    for prelude, body in rules:
        if selector in _selectors(prelude):
            value = _declarations(body).get(name, value)
    return value


# ── Less motion ──────────────────────────────────────────────────────────────


def test_system_preference_runs_every_animation_once_in_the_shell():
    # A shortened infinite animation keeps looping every frame: the mascot jittered.
    shell = _media(_css(SHELL_CSS), "prefers-reduced-motion: reduce")
    for selector in ("*", "*::before", "*::after"):
        assert _declared(shell, selector, "animation-iteration-count") == "1 !important", selector
        assert _declared(shell, selector, "animation-duration") == ".01ms !important", selector


def test_wiki_switch_still_stops_everything_and_the_mascot_does_not_override_it():
    rules = _rules(_css(SHELL_CSS))
    for selector in ('[data-reduce-motion="true"] *', '[data-reduce-motion="true"] *::before'):
        assert _declared(rules, selector, "animation") == "none !important", selector
        assert _declared(rules, selector, "transition") == "none !important", selector
    assert "!important" not in _css(MASCOT_CSS)


def test_system_preference_keeps_the_mascot_still():
    css = _css(MASCOT_CSS)
    moving = {(selector, name) for prelude, body in _style_rules(css, skip=(REDUCED,))
              for name, value in _declarations(body).items() if name in ("animation", "transition")
              and value != "none" for selector in _selectors(prelude)}
    assert {selector for selector, _ in moving} >= {
        ".mascot__body", ".mascot--hop .mascot__body", ".mascot-sprite .mascot__eyes",
        ".mascot--shades-drop .mascot__shades", ".mascot .mascot-sprite"}
    still = _media(css, "prefers-reduced-motion: reduce")
    for selector, name in moving:
        assert _declared(still, selector, name) == "none", (selector, name)
    # Same selectors, so the block has to come after the rules it switches off.
    top = _rules(css)
    last_moving = max(index for index, (prelude, body) in enumerate(top) if prelude != REDUCED
                      and not prelude.startswith("@keyframes") and re.search(r"(animation|transition)\s*:", body))
    assert [prelude for prelude, _ in top].index(REDUCED) > last_moving


# ── Touch screens ────────────────────────────────────────────────────────────


def test_touch_screens_get_a_finger_sized_mascot_clear_of_the_home_link():
    css = _css(MASCOT_CSS)
    top, coarse = _top(css), _media(css, "pointer: coarse")
    # Eleven quick taps must not turn into a double-tap zoom.
    assert _declared(top, ".mascot", "touch-action") == "manipulation"
    size = _declared(_media(_css(SHELL_CSS), "pointer: coarse"), ".btn", "min-height")
    assert size and _declared(coarse, ".mascot", "min-width") == _declared(coarse, ".mascot", "min-height") == size
    assert _declared(coarse, ".mascot", "align-items") == _declared(coarse, ".mascot", "justify-content") == "center"
    # The desktop pulls the name closer to the banana; on touch screens the full top bar gap stays.
    brand = ".mascot + .topbar__brand"
    gap = _declared(coarse, brand, "margin-inline-start") or _declared(top, brand, "margin-inline-start") or "0"
    assert not gap.startswith("-")


def test_hover_tilt_needs_a_real_hover_and_keyboard_focus_keeps_it():
    css = _css(MASCOT_CSS)
    # Touch screens keep :hover after a tap, which left the banana tilted.
    assert not [prelude for prelude, _ in _style_rules(css, skip=("@media (hover: hover)",)) if ":hover" in prelude]
    assert _declared(_media(css, "hover: hover"), ".mascot:hover .mascot-sprite", "transform") == TILT
    assert _declared(_top(css), ".mascot:focus-visible .mascot-sprite", "transform") == TILT
