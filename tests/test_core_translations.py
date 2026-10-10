"""Built-in languages: the registries agree, and every bundled text has each language.

Every ``translations/en.json`` under ``bananawiki/`` (wiki core and features,
the example plugin, the hosting portal, the desktop launcher) needs a sibling
for each built-in language with the same keys and, per key, the same
``{placeholders}``. The portal's help centre and the in-wiki user guide need
the same articles and pages as English.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bananawiki.desktop import i18n as desktop_i18n
from bananawiki.hosting import help as hosting_help
from bananawiki.hosting import i18n as hosting_i18n
from bananawiki.wiki import i18n as wiki_i18n
from bananawiki.wiki.features.auth import docs

PACKAGE = Path(__file__).resolve().parents[1] / "bananawiki"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TRANSLATED = sorted(set(wiki_i18n.BUILTIN_LANGUAGES) - {"en"})
ENGLISH_NAMES = {"de": "German", "en": "English", "it": "Italian"}
CATALOGUES = sorted(path.parent for path in PACKAGE.rglob("translations/en.json"))


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def test_every_registry_lists_the_same_languages():
    assert wiki_i18n.BUILTIN_LANGUAGES == hosting_i18n.LANGUAGES == desktop_i18n.LANGUAGES
    assert tuple(wiki_i18n.BUILTIN_LANGUAGES) == hosting_help.LANGUAGES == docs.LANGUAGES
    assert set(ENGLISH_NAMES) == set(wiki_i18n.BUILTIN_LANGUAGES)
    assert len(CATALOGUES) > 30  # the search found the feature catalogues


@pytest.mark.parametrize("language", TRANSLATED)
@pytest.mark.parametrize("folder", CATALOGUES, ids=lambda folder: folder.relative_to(PACKAGE).as_posix())
def test_catalogue_mirrors_english(folder, language):
    english = _load(folder / "en.json")
    translated = _load(folder / f"{language}.json")
    english_meta, meta = english.pop("_meta", None), translated.pop("_meta", None)
    assert sorted(set(english) - set(translated)) == [], "missing"
    assert sorted(set(translated) - set(english)) == [], "not in English"
    assert [key for key, text in english.items() if _placeholders(text) != _placeholders(translated[key])] == []
    assert [key for key, text in translated.items() if not isinstance(text, str)] == []
    assert [key for key, text in english.items() if text.strip() and not translated[key].strip()] == []
    if english_meta is not None:
        assert meta is not None
        assert meta.get("name") == wiki_i18n.BUILTIN_LANGUAGES[language]
        if "code" in english_meta:
            assert meta.get("code") == language and not meta.get("rtl")
        if "english_name" in english_meta:
            assert meta.get("english_name") == ENGLISH_NAMES[language]


@pytest.mark.parametrize("language", TRANSLATED)
def test_help_centre_mirrors_english(language):
    english = _load(hosting_help.CONTENT_DIR / "help.en.json")
    translated = _load(hosting_help.CONTENT_DIR / f"help.{language}.json")
    assert [article["slug"] for article in translated] == [article["slug"] for article in english]
    for source, article in zip(english, translated, strict=True):
        slug = source["slug"]
        assert article["title"].strip() and article["description"].strip(), slug
        assert len(article["sections"]) == len(source["sections"]), slug
        for original, section in zip(source["sections"], article["sections"], strict=True):
            assert bool(section["heading"].strip()) == bool(original["heading"].strip()), slug
            assert section["html"].strip(), slug
        assert _placeholders(json.dumps(article)) == _placeholders(json.dumps(source)), slug
    assert len(hosting_help.articles(language)) == len(english)


@pytest.mark.parametrize("language", TRANSLATED)
@pytest.mark.parametrize("variant", docs.VARIANTS)
def test_user_guide_mirrors_english(variant, language):
    english = sorted(path.name for path in (docs.DOCS_ROOT / "en" / variant).glob("*.md"))
    translated = sorted(path.name for path in (docs.DOCS_ROOT / language / variant).glob("*.md"))
    assert english and translated == english
    assert all(title and content.startswith("# ") for title, _slug, content in docs.pages(variant, language))
