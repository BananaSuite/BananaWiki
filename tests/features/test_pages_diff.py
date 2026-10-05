"""Bounded diffs: ordinary edits compare exactly, crafted inputs stay within the work budget (R-04)."""

from __future__ import annotations

import itertools
import math
import random
import time

import pytest

from bananawiki.wiki.features.pages import diff


@pytest.fixture
def spent(monkeypatch):
    """Work charged to every :class:`diff.Budget` during the test (reset it between diffs)."""
    total = [0]
    original = diff.Budget.spend

    def spend(self, steps):
        original(self, steps)
        total[0] += steps

    monkeypatch.setattr(diff.Budget, "spend", spend)
    return total


def _rebuilt(a, b, ops):
    """Check *ops* are contiguous, equal runs really match, and they turn *a* into *b*."""
    position_a = position_b = 0
    out = []
    for tag, i1, i2, j1, j2 in ops:
        assert (i1, j1) == (position_a, position_b)
        if tag == "equal":
            assert list(a[i1:i2]) == list(b[j1:j2])
        out.extend(b[j1:j2])
        position_a, position_b = i2, j2
    assert (position_a, position_b) == (len(a), len(b))
    return out


def test_ordinary_edits_are_marked_word_by_word():
    assert diff.source_diff("once upon a time", "once upon a rainy time") == ("once upon a <ins>rainy</ins> time", True)
    changed = diff.source_diff("a\nb\nc\n", "a\nB\nc\nd\n")
    assert changed.html == "a\n<del>b</del><ins>B</ins>\nc\n<ins>d</ins>\n" and changed.complete
    assert diff.source_diff("<b>", "<i>").html == "<del>&lt;b&gt;</del><ins>&lt;i&gt;</ins>"
    rendered = diff.rendered_diff("<p>once upon a time</p>\n", "<p>once upon a rainy time</p>\n")
    assert rendered.html == "<p>once upon a <ins>rainy</ins> time</p>\n" and rendered.complete
    assert diff.change_counts("a\nb\nc", "a\nB\nc\nd") == (2, 1)


def test_opcodes_are_valid_with_and_without_budget():
    rng = random.Random(4)
    for _ in range(300):
        a = [rng.choice("abcde") for _ in range(rng.randint(0, 25))]
        b = [rng.choice("abcde") for _ in range(rng.randint(0, 25))]
        for budget in (None, 3, 150):  # unlimited, nothing fits, some stretches fit
            ops, _exact = diff.opcodes(a, b, diff.Budget(budget))
            assert _rebuilt(a, b, ops) == b
            assert all(not (x[0] == y[0] == "equal") for x, y in itertools.pairwise(ops))


def test_searches_are_charged_to_the_budget(monkeypatch):
    """difflib must route every longest-match search through the bounded matcher, and each one pays."""
    calls = []
    original = diff._BoundedMatcher.find_longest_match

    def spy(self, *args):
        left = self._budget.left
        match = original(self, *args)
        assert self._budget.left < left
        calls.append(args)
        return match

    monkeypatch.setattr(diff._BoundedMatcher, "find_longest_match", spy)
    diff.opcodes(list("abcabc"), list("cbacba"), diff.Budget())  # no unique item to anchor on
    assert len(calls) > 1


def _crossing(k):
    """Every "c" of the old lines meets all its k copies in the new ones, in every search."""
    return ["c"] * k + ["p"], ["q"] + ["c", "d"] * k


def test_searches_after_the_first_are_charged_too(spent):
    """The matcher is priced on its first search, but SequenceMatcher keeps searching what is left.

    On these lines about k searches of k * k steps follow the first one: tens
    of seconds for a few kilobytes if only the first were paid for.
    """
    first_search = 50 * 51 + 1
    ops, exact = diff.opcodes(*_crossing(50), diff.Budget(diff.MATCHER_STEPS + first_search))
    assert not exact and ops == [("replace", 0, 51, 0, 101)]
    spent[0] = 0
    old, new = _crossing(math.isqrt(diff.WORK_BUDGET // 2))  # the first search takes half the budget
    ops, exact = diff.opcodes(old, new, diff.Budget())
    assert not exact and _rebuilt(old, new, ops) == new
    assert spent[0] <= diff.WORK_BUDGET


def test_long_edited_page_keeps_an_exact_diff(spent):
    """Unique paragraphs anchor the comparison, so edits far apart in a long page stay cheap."""
    paragraphs = [f"Paragraph {number} talks about bananas and wikis." for number in range(3000)]
    old = "\n\n".join(paragraphs)
    new = "\n\n".join(["Paragraph 0 talks about apples and wikis.", *paragraphs[1:-1], "The end."])
    result = diff.source_diff(old, new)
    assert result.complete
    assert "<del>bananas</del><ins>apples</ins>" in result.html and "<ins>end.</ins>" in result.html
    assert result.html.count("<ins>") == 3
    assert spent[0] <= diff.WORK_BUDGET


def test_many_small_changes_do_not_add_up_beyond_the_budget(spent, monkeypatch):
    """One matcher per small stretch cost far more than the steps it was charged: seconds for a large page."""
    built = []
    original = diff._BoundedMatcher.__init__

    def spy(self, a, b, budget):
        original(self, a, b, budget)
        built.append((len(a), len(b)))

    monkeypatch.setattr(diff._BoundedMatcher, "__init__", spy)
    # A change on one side only, or of one item for one, needs no matcher at all.
    result = diff.source_diff("".join(f"line {n}\nx\n\n" for n in range(5_000)),
                              "".join(f"line {n}\ny\n\n" for n in range(5_000)))
    assert result.complete and result.html.count("<del>x</del><ins>y</ins>") == 5_000
    assert built == []
    # Stretches that do need one pay for setting it up, so there can only be so many.
    spent[0] = 0
    stretches = diff.WORK_BUDGET // diff.MATCHER_STEPS + 1  # more set-ups than the budget pays for
    result = diff.source_diff("".join(f"line {n}\nx\nz\n" for n in range(stretches)),
                              "".join(f"line {n}\ny\nw\n" for n in range(stretches)))
    assert not result.complete
    assert 0 < len(built) <= diff.WORK_BUDGET // diff.MATCHER_STEPS
    assert spent[0] <= diff.WORK_BUDGET


PATHOLOGICAL = {
    "same-line": ("a\n" * 200_000, "b\n" + "a\n" * 199_998 + "c\n"),
    "kanban-report": ("ab\n" * 3_333, "x\n" + "ab\n" * 3_331 + "y\n"),
    "repeated-words": ("a b " * 100_000, "b a " * 100_000),
}


@pytest.mark.parametrize("case", PATHOLOGICAL)
def test_pathological_inputs_stay_within_budget(spent, case):
    """Unbounded, each of these took minutes to hours; the budget keeps them to a moment."""
    old, new = PATHOLOGICAL[case]
    started = time.monotonic()
    result = diff.source_diff(old, new)
    assert spent[0] <= diff.WORK_BUDGET
    spent[0] = 0
    diff.rendered_diff(f"<p>{old}</p>", f"<p>{new}</p>")
    assert spent[0] <= diff.WORK_BUDGET
    assert "<ins>" in result.html and "<del>" in result.html
    assert time.monotonic() - started < 30  # generous: well under a second on a laptop


def test_over_budget_comparison_is_coarse_and_says_so(spent):
    result = diff.source_diff("a\n" * 200_000, "b\n" + "a\n" * 199_998 + "c\n")
    assert not result.complete
    assert result.html.startswith("<del>") and result.html.count("<del>") == 1 and result.html.count("<ins>") == 1
    ops, exact = diff.opcodes(["a"] * 5_000, ["a"] * 4_999 + ["b"], diff.Budget())
    assert exact  # a common beginning and end never cost anything
    assert ops == [("equal", 0, 4_999, 0, 4_999), ("replace", 4_999, 5_000, 4_999, 5_000)]
