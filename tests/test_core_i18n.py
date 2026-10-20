"""The translation catalogue: uploaded languages are noticed without listing their folder on every string."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bananawiki.core import i18n
from bananawiki.core.i18n import Catalog


@pytest.fixture
def catalog(tmp_path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "en.json").write_text(json.dumps({"nav.home": "Home"}), encoding="utf-8")
    return Catalog([bundled], custom_dir=str(tmp_path / "custom"))


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(i18n.time, "monotonic", lambda: now[0])
    return now


def write_language(catalog: Catalog, code: str, text: str) -> None:
    catalog.custom_dir.mkdir(exist_ok=True)
    (catalog.custom_dir / f"{code}.json").write_text(json.dumps({"nav.home": text}), encoding="utf-8")


def test_translations_do_not_list_the_language_folder_for_every_string(catalog, clock, monkeypatch):
    write_language(catalog, "fr", "Accueil")
    listed = []
    real_glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: listed.append(self) or real_glob(self, pattern))
    for _ in range(500):
        assert catalog.translate("fr", "nav.home") == "Accueil"
    assert len(listed) == 1


def test_a_language_changed_by_another_worker_is_picked_up_after_a_few_seconds(catalog, clock):
    write_language(catalog, "fr", "Accueil")
    assert catalog.translate("fr", "nav.home") == "Accueil"
    write_language(catalog, "fr", "Page d'accueil")  # another process, so no reload() here
    assert catalog.translate("fr", "nav.home") == "Accueil"
    clock[0] += i18n.CUSTOM_CHECK_SECONDS
    assert catalog.translate("fr", "nav.home") == "Page d'accueil"


def test_the_worker_that_uploads_or_deletes_a_language_sees_it_at_once(catalog, clock):
    assert "fr" not in catalog.languages()
    catalog.write_custom("fr", {"nav.home": "Accueil"})
    assert catalog.translate("fr", "nav.home") == "Accueil"
    assert catalog.delete_custom("fr")
    assert catalog.translate("fr", "nav.home") == "Home"
