"""A translated label must not be followed by a stray closing brace.

`{{ t('key') }}}` renders the label and then a literal "}", and so does a
Jinja comment closed with `#}}`. Both had crept into two admin pages eighteen
times. Braces that close CSS rules inside <style> are
legitimate and are not matched here, because they follow a value, not a t()
call.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_translation_call_is_followed_by_a_stray_brace():
    # A t() call, filtered or not, closed with one brace too many, or a
    # comment closed with one.
    pattern = re.compile(r"\{\{\s*t\(.*?\)[^{}]*\}\}\}|#\}\}")
    offenders = []
    for folder in ("app/templates", "hosting/templates", "plugins"):
        for template in (ROOT / folder).rglob("*.html"):
            for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{template.relative_to(ROOT)}:{number}")
    assert not offenders, offenders
