"""Find the Pygments lexers that stay slow on short pieces of code (bananawiki.wiki.markdown).

Markdown highlighting lexes code in pieces of HIGHLIGHT_CHUNK_CHARS and checks
its time between tokens, which bounds every lexer whose slowest token on such a
piece is short. This script runs every lexer of the installed Pygments on
adversarial pieces of that size (runs of one or two characters, long blank
runs after a keyword, ...) and prints those whose slowest token takes longer
than --limit seconds: they belong in markdown._UNBOUNDED_LEXERS. Run it after
upgrading Pygments, then update that set and MEASURED_PYGMENTS_VERSION.

    python scripts/measure_lexers.py [--size 1024] [--limit 0.1] [--jobs 8]

Unix only (a lexer that needs more than --timeout seconds is stopped by SIGALRM).
"""

from __future__ import annotations

import argparse
import multiprocessing
import signal
import time

import pygments
from pygments.lexers import get_all_lexers, get_lexer_by_name

_UNITS = list("\"'\\/*<>(){}[]#%0a \t$`-:.=@!^&|?;,~+\n") + [
    "x\n", " \n", "a ", ". ", "\\a", "/*", "*/", "<!", "a=", "=\n", "#\n", "-\n", "--", "//", "{%", "%}", "{{",
    "${", "#{", "a(", "a.", "0.", "0x", "\\\n", "'\n", '"\n', "a:", ":=", "->", "=>", ";\n", "(\n", "a\t", "<a ",
    "$ x\n", ">>> x\n", "In [1]: x\n", "% x\n", "> x\n", "a,", "1e", "@a", "!a", "a\\",
]
_HEADS = ("def ", "class ", "function ", "var ", "SELECT ", "<div ", "#include ", "import ", "let ", "[a]\n")


class _Timeout(Exception):
    pass


def inputs(size: int) -> list[str]:
    out = [(unit * (size // len(unit) + 1))[:size] for unit in _UNITS]
    out += ["#" + " " * (size - 2) + "x", '"' + "\\a" * ((size - 1) // 2), "/*" + "*" * (size - 2),
            "x" + "\n" * (size - 1), ("def" + " \n" * size)[:size], ("def" + "\n" * size)[:size]]
    for head in _HEADS:
        out += [head + "a" * (size - len(head)), head + " " * (size - len(head) - 1) + "x"]
    return out


def _alarm(*_args: object) -> None:
    raise _Timeout


def slowest_token(name: str, size: int, timeout: float) -> tuple[str, float]:
    """The longest wait for one token of lexer *name* over :func:`inputs` (processor time)."""
    signal.signal(signal.SIGALRM, _alarm)
    lexer = get_lexer_by_name(name, stripall=False)
    worst = 0.0
    for text in inputs(size):
        signal.setitimer(signal.ITIMER_REAL, timeout)
        last = time.thread_time()
        try:
            for _token in lexer.get_tokens(text):
                now = time.thread_time()
                worst = max(worst, now - last)
                last = now
        except _Timeout:
            return name, float("inf")
        except Exception:  # noqa: BLE001, S112 - a lexer failing on nonsense input is not slow
            continue
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return name, worst


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--limit", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=multiprocessing.cpu_count())
    options = parser.parse_args()
    names = sorted(aliases[0] for _name, aliases, *_rest in get_all_lexers() if aliases)
    with multiprocessing.Pool(options.jobs) as pool:
        results = pool.starmap(slowest_token, [(name, options.size, options.timeout) for name in names])
    print(f"Pygments {pygments.__version__}: {len(names)} lexers, pieces of {options.size} characters")
    for name, worst in sorted(results, key=lambda result: -result[1]):
        if worst > options.limit:
            print(f"{get_lexer_by_name(name).__class__.__name__}\t{name}\t{worst:.3f} s")


if __name__ == "__main__":
    main()
