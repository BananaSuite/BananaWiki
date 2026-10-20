"""Edit summaries the wiki writes itself, shown in the reader's language.

A page revision saved by the wiki (a revert, a rename, an import, the page
builder, a contribution approval) stores a fixed English summary, which the
REST API and data exports return as stored. Page history, the profile and the
admin dashboard pass every summary through :func:`render`: a summary that
matches one of these texts is translated, anything a person typed is shown
unchanged.
"""

from __future__ import annotations

import re

from ...i18n import t

FIXED = {
    "Created": "pages.summary.created",
    "Created via API": "pages.summary.created_api",
    "Edited via API": "pages.summary.edited_api",
    "Imported from Markdown": "pages.summary.imported",
    "Updated from Markdown import": "pages.summary.import_updated",
    "Updated with the visual page builder": "pages.summary.page_builder",
    "Built-in documentation": "pages.summary.builtin_docs",
}

PATTERNS = (
    (re.compile(r"Title changed from '(?P<title>.+)'", re.S), "pages.summary.title_changed"),
    (re.compile(r"Reverted to revision (?P<revision>\d+)"), "pages.summary.reverted"),
    (re.compile(r"Updated links to (?P<address>/page/\S+)"), "pages.summary.links_updated"),
    (re.compile(r"Updated mention of @(?P<username>\S+)"), "pages.summary.mention_updated"),
    (re.compile(r"Forked from (?P<wiki>.+) \((?P<peer>[^()\s]+)\)", re.S), "pages.summary.forked"),
    (re.compile(r"Contribution #(?P<number>\d+) by (?P<author>\S+), approved by (?P<reviewer>\S+): (?P<reason>.*)",
                re.S), "pages.summary.contribution"),
    (re.compile(r"Contributors: (?P<names>.+)", re.S), "pages.summary.contributors"),
)

# Drafts append the people who helped to the summary of the edit that saved them.
_CONTRIBUTORS = re.compile(r"(?P<summary>.+) \(contributors: (?P<names>[^()]+)\)", re.S)


def render(message: str | None) -> str:
    text = message or ""
    key = FIXED.get(text)
    if key:
        return t(key)
    for pattern, key in PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return t(key, **match.groupdict())
    match = _CONTRIBUTORS.fullmatch(text)
    if match:
        return t("pages.summary.with_contributors", summary=render(match["summary"]), names=match["names"])
    return text
