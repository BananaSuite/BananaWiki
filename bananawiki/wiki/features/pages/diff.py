"""Differences between two versions of a text (page history, edit conflicts, reviews, tickets).

Every comparison runs under a work budget. :class:`difflib.SequenceMatcher`
with autojunk off may take ``len(a) * len(b)`` steps when tokens repeat, so a
crafted page or proposal could otherwise keep a worker busy for hours. Texts
are compared line by line, then word by word inside the changed lines while
the budget lasts. When even the lines cost too much, the stretch that could
not be compared is shown as removed and re-added as a whole, and the result
says it is not ``complete`` so the page can tell the reader.
"""

from __future__ import annotations

import bisect
import difflib
import itertools
import re
from collections import Counter
from collections.abc import Callable, Hashable, Iterator, Sequence
from typing import NamedTuple

from markupsafe import Markup, escape

from ... import markdown

# Steps of SequenceMatcher's inner loop one diff may take: well under a second of CPU.
WORK_BUDGET = 1_000_000
# What setting up one comparison costs beyond its items, in the same steps (measured).
MATCHER_STEPS = 64
_WORDS = re.compile(r"(\s+)")
_TAGS = re.compile(r"(<[^<>]+>)")

Opcode = tuple[str, int, int, int, int]
Run = tuple[str, list[str], list[str]]


class Diff(NamedTuple):
    """Marked-up differences; ``complete`` is False when only a coarse comparison fitted the budget."""

    html: Markup
    complete: bool


class _OverBudget(Exception):
    pass


class Budget:
    """Work allowance shared by the comparisons behind one diff."""

    def __init__(self, steps: int | None = None):
        self.left = WORK_BUDGET if steps is None else steps

    def spend(self, steps: int) -> None:
        if steps > self.left:
            raise _OverBudget
        self.left -= steps


class _BoundedMatcher(difflib.SequenceMatcher):
    """A SequenceMatcher that pays for every longest-match search before running it.

    A search over ``a[alo:ahi]`` visits, for each of those elements, at most
    every position where it occurs in ``b``. Prefix sums of these counts price
    any search in constant time, so the total work never exceeds the budget.
    Setting a matcher up has a fixed price too, so many small comparisons
    cannot cost more than one large one. (Indexing the items themselves is
    not charged: the compared stretches never overlap, so it adds up to one
    pass over the texts.)
    """

    def __init__(self, a: Sequence[Hashable], b: Sequence[Hashable], budget: Budget):
        budget.spend(MATCHER_STEPS)
        occurrences = Counter(b)
        self._cost = [0, *itertools.accumulate(occurrences[item] + 1 for item in a)]
        if self._cost[-1] > budget.left:  # the first search spans everything: do not even index b
            raise _OverBudget
        self._budget = budget
        super().__init__(None, a, b, autojunk=False)

    def find_longest_match(self, alo: int = 0, ahi: int | None = None, blo: int = 0, bhi: int | None = None):
        ahi = len(self.a) if ahi is None else ahi
        self._budget.spend(self._cost[ahi] - self._cost[alo])
        return super().find_longest_match(alo, ahi, blo, bhi)


def _change(i1: int, i2: int, j1: int, j2: int) -> list[Opcode]:
    if i1 == i2 and j1 == j2:
        return []
    tag = "replace" if i1 < i2 and j1 < j2 else "delete" if i1 < i2 else "insert"
    return [(tag, i1, i2, j1, j2)]


def _append(ops: list[Opcode], op: Opcode) -> None:
    """Add *op*, merging it into the previous one when both are unchanged runs."""
    if ops and op[0] == "equal" and ops[-1][0] == "equal":
        tag, i1, _i2, j1, _j2 = ops[-1]
        ops[-1] = (tag, i1, op[2], j1, op[4])
    else:
        ops.append(op)


def _anchors(a: Sequence[Hashable], b: Sequence[Hashable]) -> list[tuple[int, int]]:
    """Positions ``(i, j)`` of items found exactly once in *a* and in *b*, in the same order on both sides.

    Such items (most paragraphs, most rare words) pin the comparison down, so
    the costly matching only runs on the short stretches between them, as in
    a patience diff. The longest increasing run is found by patience sorting.
    """
    count_a, count_b = Counter(a), Counter(b)
    where_b = {item: j for j, item in enumerate(b) if count_b[item] == 1}
    pairs = [(i, where_b[item]) for i, item in enumerate(a) if count_a[item] == 1 and item in where_b]
    tails: list[int] = []
    ends: list[int] = []
    before = [-1] * len(pairs)
    for index, (_i, j) in enumerate(pairs):
        k = bisect.bisect_left(tails, j)
        if k:
            before[index] = ends[k - 1]
        if k == len(tails):
            tails.append(j)
            ends.append(index)
        else:
            tails[k], ends[k] = j, index
    chain: list[tuple[int, int]] = []
    index = ends[-1] if ends else -1
    while index >= 0:
        chain.append(pairs[index])
        index = before[index]
    return chain[::-1]


def _ends(a: Sequence[Hashable], b: Sequence[Hashable]) -> tuple[int, int]:
    """Lengths of the common beginning and of the common end of *a* and *b* (not overlapping)."""
    size_a, size_b = len(a), len(b)
    shortest = min(size_a, size_b)
    head = 0
    while head < shortest and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < shortest - head and a[size_a - 1 - tail] == b[size_b - 1 - tail]:
        tail += 1
    return head, tail


def _compare(a: Sequence[Hashable], b: Sequence[Hashable], budget: Budget, *,
             anchored: bool) -> tuple[list[Opcode], bool]:
    """Opcodes turning *a* into *b*, and whether they are exact (see :func:`opcodes`)."""
    size_a, size_b = len(a), len(b)
    head, tail = _ends(a, b)
    i2, j2 = size_a - tail, size_b - tail
    ops: list[Opcode] = [("equal", 0, head, 0, head)] if head else []
    exact = True
    if i2 == head or j2 == head or (i2 - head == 1 and j2 - head == 1):
        ops.extend(_change(head, i2, head, j2))  # one side only, or one item each: nothing to search
    elif anchored:
        i0 = j0 = head
        for i, j in [*((head + i, head + j) for i, j in _anchors(a[head:i2], b[head:j2])), (i2, j2)]:
            if i > i0 or j > j0:
                stretch, fits = _compare(a[i0:i], b[j0:j], budget, anchored=False)
                exact = exact and fits
                for tag, x1, x2, y1, y2 in stretch:
                    _append(ops, (tag, x1 + i0, x2 + i0, y1 + j0, y2 + j0))
            if i < i2:
                _append(ops, ("equal", i, i + 1, j, j + 1))
            i0, j0 = i + 1, j + 1
    else:
        try:
            matched = _BoundedMatcher(a[head:i2], b[head:j2], budget).get_opcodes()
        except _OverBudget:
            matched, exact = _change(0, i2 - head, 0, j2 - head), False
        ops.extend((tag, x1 + head, x2 + head, y1 + head, y2 + head) for tag, x1, x2, y1, y2 in matched)
    if tail:
        _append(ops, ("equal", i2, size_a, j2, size_b))
    return ops, exact


def opcodes(a: Sequence[Hashable], b: Sequence[Hashable], budget: Budget) -> tuple[list[Opcode], bool]:
    """SequenceMatcher-style opcodes turning *a* into *b*, and whether they are exact.

    The common beginning and end (most edits touch one spot of a long text)
    and the items unique to both sides match directly. Each stretch between
    them is trimmed the same way, and what remains of it is a plain change
    when it is on one side only or one item each; SequenceMatcher only
    compares the rest. A stretch whose comparison would overrun *budget*
    becomes a single change and the opcodes are not exact.
    """
    return _compare(a, b, budget, anchored=True)


def grouped_opcodes(ops: list[Opcode], context: int = 3) -> Iterator[list[Opcode]]:
    """Changes with up to *context* unchanged items around them, as ``SequenceMatcher.get_grouped_opcodes``."""
    codes = list(ops) or [("equal", 0, 1, 0, 1)]
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)
    group: list[Opcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * context:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def _runs(old_lines: list[str], new_lines: list[str], words: Callable[[str], list[str]],
          chunks: Callable[[str], list[str]]) -> tuple[list[Run], bool]:
    """``(tag, old tokens, new tokens)``: lines compared first, then words inside each changed run.

    Equal runs carry whole lines and changed runs carry words, or the coarser
    *chunks* when matching those words would overrun the budget (or matching
    the lines already did). Each replaced run pays for setting up its word
    comparison, so a text of many small changes is not refined without end.
    """
    budget = Budget()
    ops, complete = opcodes(old_lines, new_lines, budget)
    runs: list[Run] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            runs.append((tag, old_lines[i1:i2], new_lines[j1:j2]))
            continue
        old_text, new_text = "".join(old_lines[i1:i2]), "".join(new_lines[j1:j2])
        if complete and tag != "replace":
            runs.append((tag, words(old_text), words(new_text)))
            continue
        if complete and budget.left >= MATCHER_STEPS:
            budget.spend(MATCHER_STEPS)
            old_words, new_words = words(old_text), words(new_text)
            inner, exact = opcodes(old_words, new_words, budget)
            if exact:
                runs.extend((inner_tag, old_words[a1:a2], new_words[b1:b2]) for inner_tag, a1, a2, b1, b2 in inner)
                continue
        runs.append((tag, chunks(old_text), chunks(new_text)))
    return runs, complete


def source_diff(old: str | None, new: str | None) -> Diff:
    """The new Markdown source with insertions and deletions marked (escaped HTML)."""
    runs, complete = _runs((old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
                           _WORDS.split, lambda text: [text])
    parts: list[str] = []

    def mark(tokens: list[str], tag: str) -> None:
        for token in tokens:
            parts.append(f"<{tag}>{escape(token)}</{tag}>" if token.strip() else str(escape(token)))

    for op, old_tokens, new_tokens in runs:
        if op == "equal":
            parts.append(str(escape("".join(new_tokens))))
            continue
        if op in ("delete", "replace"):
            mark(old_tokens, "del")
        if op in ("insert", "replace"):
            mark(new_tokens, "ins")
    return Diff(Markup("".join(parts)), complete)


def _html_lines(html: str) -> list[str]:
    """Rendered HTML cut after each newline outside a tag, so no tag is split."""
    lines: list[str] = []
    current: list[str] = []
    for piece in _TAGS.split(html):
        if piece.startswith("<"):
            current.append(piece)
            continue
        *ended, rest = piece.split("\n")
        for text in ended:
            current.append(text + "\n")
            lines.append("".join(current))
            current = []
        current.append(rest)
    last = "".join(current)
    if last:
        lines.append(last)
    return lines


def _html_tokens(html: str) -> list[str]:
    """Tags and single words (with their whitespace) of rendered HTML."""
    tokens: list[str] = []
    for piece in _TAGS.split(html):
        if piece.startswith("<"):
            tokens.append(piece)
        elif piece:
            tokens.extend(part for part in _WORDS.split(piece) if part)
    return tokens


def _html_chunks(html: str) -> list[str]:
    """Tags and the whole texts between them."""
    return [piece for piece in _TAGS.split(html) if piece]


def rendered_diff(old_html: str, new_html: str) -> Diff:
    """Rendered HTML of the new version with changed text marked.

    Structure comes from the new version; deleted text from the old one is
    shown struck through. The result is sanitised again, because splicing
    fragments of two documents could otherwise produce unexpected markup.
    """
    runs, complete = _runs(_html_lines(old_html or ""), _html_lines(new_html or ""), _html_tokens, _html_chunks)
    parts: list[str] = []
    for op, old_tokens, new_tokens in runs:
        if op == "equal":
            parts.extend(new_tokens)
            continue
        if op in ("delete", "replace"):
            for token in old_tokens:
                if not token.startswith("<"):
                    parts.append(f"<del>{token}</del>" if token.strip() else token)
        if op in ("insert", "replace"):
            for token in new_tokens:
                if token.startswith("<") or not token.strip():
                    parts.append(token)
                else:
                    parts.append(f"<ins>{token}</ins>")
    return Diff(Markup(markdown.sanitize("".join(parts))), complete)


def change_counts(old: str | None, new: str | None) -> tuple[int, int]:
    """(added, removed) line counts between two versions."""
    ops, _exact = opcodes((old or "").splitlines(), (new or "").splitlines(), Budget())
    added = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in ops if tag != "equal")
    removed = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag != "equal")
    return added, removed
