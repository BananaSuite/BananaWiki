"""Finding the Save buttons of a rendered page, for the tests that keep one Save per settings page.

A settings page has one Save, at the top right in its sticky header (``page-header--save``): never one at the
bottom, one per section, or a second one hidden with CSS. A Save here is a submit button whose label starts
with Save, Update or Apply (in English, the language the tests render in). Left out: a button inside
``<noscript>``, which stands in for a switch that applies on change (``data-autosubmit``) and only shows without
scripts, and the buttons of search and filter forms (``method="get"``), which change nothing.
"""

from __future__ import annotations

import html as html_lib
import re

SAVE_LABEL = re.compile(r"^(save|update|apply)\b", re.I)
_NOSCRIPT = re.compile(r"<noscript\b.*?</noscript>", re.S | re.I)
_GET_FORM = re.compile(r"<form\b[^>]*\bmethod=[\"']get[\"'][^>]*>.*?</form>", re.S | re.I)
_BUTTON = re.compile(r"<button\b([^>]*)>(.*?)</button>", re.S | re.I)
_SUBMIT_INPUT = re.compile(r"<input\b([^>]*\btype=[\"']submit[\"'][^>]*)>", re.I)
_SAVE_HEADER = re.compile(r"<(header|div)\b[^>]*\bclass=\"[^\"]*\bpage-header--save\b[^\"]*\"[^>]*>", re.I)


def _blank(match: re.Match) -> str:
    return " " * len(match.group(0))  # keeps every other position in the page where it was


def _text(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def save_controls(page: str) -> list[tuple[int, str]]:
    """``(position, label)`` of each Save of *page*, in page order."""
    page = _GET_FORM.sub(_blank, _NOSCRIPT.sub(_blank, page))
    found = []
    for match in _BUTTON.finditer(page):
        attrs, label = match.group(1), _text(match.group(2))
        if re.search(r"\btype=[\"'](button|reset)[\"']", attrs, re.I):
            continue
        if SAVE_LABEL.match(label):
            found.append((match.start(), label))
    for match in _SUBMIT_INPUT.finditer(page):
        value = re.search(r"\bvalue=\"([^\"]*)\"", match.group(1))
        if value and SAVE_LABEL.match(html_lib.unescape(value.group(1)).strip()):
            found.append((match.start(), value.group(1)))
    return sorted(found)


def save_header(page: str) -> tuple[int, int] | None:
    """Where the page's ``page-header--save`` header starts and ends, if it has one."""
    start = _SAVE_HEADER.search(page)
    if start is None:
        return None
    tag = start.group(1).lower()
    depth, position = 0, start.start()
    for match in re.finditer(rf"<(/?){tag}\b[^>]*>", page[start.start():], re.I):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return start.start(), start.start() + match.end()
        position = match.end()
    raise AssertionError(f"the save header is not closed after position {position}")


def assert_one_save_in_header(page: str, where: str = "") -> None:
    """At most one Save on *page*, and that one inside its sticky header, before any of the page's content."""
    controls = save_controls(page)
    assert len(controls) <= 1, f"{where}: more than one Save: {[label for _, label in controls]}"
    if not controls:
        return
    header = save_header(page)
    assert header is not None, f"{where}: the Save {controls[0][1]!r} is not in a page-header--save header"
    assert header[0] <= controls[0][0] < header[1], f"{where}: the Save {controls[0][1]!r} is outside the page header"


_DIALOG = re.compile(r"<dialog\b.*?</dialog>", re.S | re.I)
_PRIMARY = re.compile(r"<(?:button|a|input)\b[^>]*\bclass=\"[^\"]*\bbtn--primary\b[^\"]*\"[^>]*>", re.I)


def assert_save_is_the_only_primary(page: str, where: str = "") -> None:
    """On a page with a Save header, that Save is the page's one primary action: no other button stands out.

    A dialog's own buttons are left out: it covers the page while it is open, and its action is the primary there.
    """
    page = _DIALOG.sub(_blank, _NOSCRIPT.sub(_blank, page))
    header = save_header(page)
    if header is None or not save_controls(page):
        return
    outside = [match.group(0) for match in _PRIMARY.finditer(page) if not header[0] <= match.start() < header[1]]
    assert not outside, f"{where}: primary buttons besides the Save: {outside}"
