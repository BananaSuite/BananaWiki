"""The example environment file lists every variable the configuration reference documents, and the reference
documents the variables the code derives from its language list."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"`((?:BW|HOSTING)_[A-Z0-9_]*[A-Z0-9])`")
TABLE_NAME = re.compile(r"`([A-Z][A-Z0-9]*_[A-Z0-9_]*[A-Z0-9])`")


def test_example_lists_every_documented_variable():
    reference = (ROOT / "docs" / "configuration.md").read_text().split("## Removed variables")[0]
    example = (ROOT / ".env.example").read_text()
    documented = set(NAME.findall(reference)) - {"BW_DB_OBSERVABILITY"}
    missing = sorted(name for name in documented if f"# {name}=" not in example)
    assert not missing, f"missing from .env.example: {missing}"


def test_example_lists_every_variable_of_the_reference_tables():
    """Table rows also document names without a prefix, such as the hosting portal's BASE_DOMAIN. A name given
    with an alias is listed under either name; names that share a row are each listed."""
    reference = (ROOT / "docs" / "configuration.md").read_text().split("## Removed variables")[0]
    example = (ROOT / ".env.example").read_text()
    missing = []
    for row in reference.splitlines():
        if not row.startswith("| `"):
            continue
        cell = row.split("|")[1]
        names = TABLE_NAME.findall(cell)
        unlisted = [name for name in names if f"# {name}=" not in example]
        if "(alias " in cell and len(unlisted) < len(names):
            continue
        missing += unlisted
    assert "BASE_DOMAIN" in reference and "# BASE_DOMAIN=" in example
    assert not missing, f"missing from .env.example: {sorted(missing)}"


def test_example_names_only_variables_the_reference_documents():
    reference = (ROOT / "docs" / "configuration.md").read_text()
    example = (ROOT / ".env.example").read_text()
    unknown = sorted(name for name in re.findall(r"^# ([A-Z][A-Z0-9_]+)=", example, re.M)
                     if f"`{name}`" not in reference)
    assert not unknown, f"not in docs/configuration.md: {unknown}"


def test_reference_documents_the_legal_page_pair_of_every_portal_language():
    """The portal reads HOSTING_TERMS_URL_<LANG> and HOSTING_PRIVACY_URL_<LANG> for each of its interface
    languages but English (hosting.config._legal_urls), so a new language needs its pair documented."""
    from bananawiki.core.i18n import FALLBACK
    from bananawiki.hosting.i18n import LANGUAGES

    reference = (ROOT / "docs" / "configuration.md").read_text().split("## Removed variables")[0]
    expected = {f"HOSTING_{page}_URL_{code.upper()}" for page in ("TERMS", "PRIVACY")
                for code in LANGUAGES if code != FALLBACK}
    assert "HOSTING_TERMS_URL_DE" in expected
    missing = sorted(name for name in expected if f"`{name}`" not in reference)
    assert not missing, f"not in docs/configuration.md: {missing}"
