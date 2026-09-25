"""Sync the on-disk user guide Markdown files from ``db._wiki_docs``.

The canonical source of truth for BananaWiki's in-app user guide is the
``db._wiki_docs`` module: the same content that gets spawned into the
**BananaWiki** category and packaged into the *Download docs* ZIP.

This script regenerates the matching Markdown files under
``docs/user-guide/<lang>/<slug>.md`` so the on-disk reference never
drifts away from what users actually see inside the wiki.

It is exercised in CI by ``tests/test_wiki_docs.py::TestDocsFilesInSync``
which runs the same regeneration in-memory and fails if any file on disk
is missing or out of date.  Run this script after editing
``db/_wiki_docs.py`` and commit the resulting changes.

Usage::

    python scripts/sync_user_guide_docs.py            # write files
    python scripts/sync_user_guide_docs.py --check    # exit 1 on diff
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Iterable, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db._wiki_docs import get_docs_pages  # noqa: E402  (sys.path tweak above)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_GUIDE_DIR = os.path.join(REPO_ROOT, "docs", "user-guide")

# (language, simplified) -> sub-directory under docs/user-guide/.
DOC_SETS: Tuple[Tuple[str, bool, str], ...] = (
    ("en", False, "en"),
    ("it", False, "it"),
)


def _index_intro(language: str) -> str:
    if language == "it":
        return (
            "# Guida utente di BananaWiki\n\n"
            "Questa cartella contiene la versione su disco della guida "
            "utente di BananaWiki. È lo stesso testo che viene creato "
            "all'interno della wiki dalla funzione **Spawn documentation** "
            "(Admin → Impostazioni del sito → Wiki Documentation), e "
            "viene generato automaticamente dal modulo Python "
            "`db/_wiki_docs.py` tramite "
            "`scripts/sync_user_guide_docs.py`.\n\n"
            "Per modificare la guida:\n\n"
            "1. Modifica direttamente i file `.md` di questa cartella, "
            "oppure modifica le stringhe dentro `db/_wiki_docs.py`.\n"
            "2. Esegui `python scripts/sync_user_guide_docs.py` per "
            "riallineare i due lati (la sorgente Python è il riferimento "
            "ufficiale).\n"
            "3. Per pubblicare le modifiche dentro una wiki reale, usa "
            "**Admin → Site Settings → Wiki Documentation → Download "
            "ZIP**, modifica i file in locale e re-importali con "
            "**Bulk Markdown Import**, oppure ri-spawna la "
            "documentazione standard.\n\n"
            "## Pagine\n\n"
        )
    return (
        "# BananaWiki user guide\n\n"
        "This folder is the on-disk mirror of the BananaWiki user "
        "guide.  It is the exact text that **Spawn documentation** "
        "(Admin → Site Settings → Wiki Documentation) writes into the "
        "wiki, and it is generated automatically from the Python "
        "module `db/_wiki_docs.py` via "
        "`scripts/sync_user_guide_docs.py`.\n\n"
        "To edit the guide:\n\n"
        "1. Edit the Markdown files in this folder directly, or edit "
        "the strings in `db/_wiki_docs.py`.\n"
        "2. Run `python scripts/sync_user_guide_docs.py` to keep both "
        "sides in sync (the Python source is the canonical reference).\n"
        "3. To publish your edits inside a real wiki, use **Admin → "
        "Site Settings → Wiki Documentation → Download ZIP**, edit the "
        "Markdown files locally, then re-upload with **Bulk Markdown "
        "Import**, or re-spawn the stock documentation.\n\n"
        "## Pages\n\n"
    )


def _build_index(language: str, pages: Iterable[Tuple[str, str, str]]) -> str:
    lines: List[str] = [_index_intro(language)]
    for title, slug, _content in pages:
        lines.append(f"- [{title}]({slug}.md)\n")
    return "".join(lines)


def render_user_guide() -> Dict[str, str]:
    """Return ``{relative_path: file_contents}`` for every guide file.

    Paths are relative to the repository root (e.g. ``docs/user-guide/en/welcome.md``).
    """
    files: Dict[str, str] = {}
    for language, simplified, subdir in DOC_SETS:
        pages = get_docs_pages(simplified=simplified, language=language)
        folder = os.path.join("docs", "user-guide", subdir)
        # Per-page files.
        for _title, slug, content in pages:
            normalised = content if content.endswith("\n") else content + "\n"
            files[os.path.join(folder, f"{slug}.md")] = normalised
        # Index.
        files[os.path.join(folder, "README.md")] = _build_index(language, pages)
    return files


def _write_files(files: Dict[str, str]) -> List[str]:
    written: List[str] = []
    for rel_path, contents in files.items():
        abs_path = os.path.join(REPO_ROOT, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        existing: str = ""
        if os.path.exists(abs_path):
            with open(abs_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        if existing != contents:
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(contents)
            written.append(rel_path)
    return written


def _check_files(files: Dict[str, str]) -> List[str]:
    drift: List[str] = []
    for rel_path, contents in files.items():
        abs_path = os.path.join(REPO_ROOT, rel_path)
        if not os.path.exists(abs_path):
            drift.append(rel_path)
            continue
        with open(abs_path, "r", encoding="utf-8") as fh:
            if fh.read() != contents:
                drift.append(rel_path)
    return drift


def _existing_user_guide_paths() -> List[str]:
    paths: List[str] = []
    if not os.path.isdir(USER_GUIDE_DIR):
        return paths
    for root, _dirs, files in os.walk(USER_GUIDE_DIR):
        for name in files:
            if name.endswith(".md"):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, REPO_ROOT)
                paths.append(rel.replace(os.sep, "/"))
    return paths


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any on-disk file would change.",
    )
    args = parser.parse_args(argv)

    expected = render_user_guide()
    expected_paths = {p.replace(os.sep, "/") for p in expected.keys()}

    if args.check:
        drift = _check_files(expected)
        unexpected = [
            p for p in _existing_user_guide_paths() if p not in expected_paths
        ]
        if drift or unexpected:
            print("docs/user-guide/ is out of sync with db/_wiki_docs.py:")
            for path in drift:
                print(f"  changed/missing: {path}")
            for path in unexpected:
                print(f"  unexpected file: {path}")
            print(
                "\nRun `python scripts/sync_user_guide_docs.py` to "
                "regenerate the on-disk user guide."
            )
            return 1
        print("docs/user-guide/ is in sync.")
        return 0

    written = _write_files(expected)
    # Remove stale files that no longer correspond to any page.
    removed: List[str] = []
    for path in _existing_user_guide_paths():
        if path not in expected_paths:
            os.remove(os.path.join(REPO_ROOT, path))
            removed.append(path)

    if not written and not removed:
        print("docs/user-guide/ already up to date.")
    else:
        for path in written:
            print(f"wrote {path}")
        for path in removed:
            print(f"removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
