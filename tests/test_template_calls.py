"""Guard against template call syntax that only fails when the page is opened.

Jinja resolves ``t('key', default('text'))`` as a call to an undefined
``default`` function, so the whole page returns 500. The working form passes
``default`` as a keyword. Nothing else catches this until someone loads the
page, which is how it reached a release candidate.
"""

import pathlib
import re

TEMPLATE_DIRS = ["app/templates", "hosting/templates", "plugins"]
BROKEN_DEFAULT = re.compile(r",\s*default\(")


def _templates():
    root = pathlib.Path(__file__).resolve().parent.parent
    for directory in TEMPLATE_DIRS:
        base = root / directory
        if base.is_dir():
            yield from base.rglob("*.html")


def test_no_default_passed_as_a_call():
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.split("\n"), 1):
            if BROKEN_DEFAULT.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, "use default='text', not default('text'):\n" + "\n".join(offenders)
