"""Mascot texts: one account or many in the administrator's results, and what "reset" keeps."""

import json
import re
from pathlib import Path

import pytest

from bananawiki.wiki.features.mascot import service
from bananawiki.wiki.i18n import BUILTIN_LANGUAGES

FOLDER = Path(__file__).resolve().parents[2] / "bananawiki/wiki/features/mascot/translations"

# What each administrator action reports, for one account and for several.
RESULTS = {
    "de": {"hide": ("Maskottchen für 1 Konto ausgeblendet.", "Maskottchen für {} Konten ausgeblendet."),
           "show": ("Maskottchen für 1 Konto angezeigt.", "Maskottchen für {} Konten angezeigt."),
           "shades_on": ("Sonnenbrille für 1 Konto aufgesetzt.", "Sonnenbrille für {} Konten aufgesetzt."),
           "shades_off": ("Sonnenbrille für 1 Konto abgenommen.", "Sonnenbrille für {} Konten abgenommen.")},
    "en": {"hide": ("Mascot hidden on 1 account.", "Mascot hidden on {} accounts."),
           "show": ("Mascot shown on 1 account.", "Mascot shown on {} accounts."),
           "shades_on": ("Sunglasses put on for 1 account.", "Sunglasses put on for {} accounts."),
           "shades_off": ("Sunglasses taken off on 1 account.", "Sunglasses taken off on {} accounts.")},
    "it": {"hide": ("Mascotte nascosta su 1 account.", "Mascotte nascosta su {} account."),
           "show": ("Mascotte mostrata su 1 account.", "Mascotte mostrata su {} account."),
           "shades_on": ("Occhiali messi su 1 account.", "Occhiali messi su {} account."),
           "shades_off": ("Occhiali tolti su 1 account.", "Occhiali tolti su {} account.")},
}
# The action that undoes each one.
OPPOSITE = {"hide": "show", "show": "hide", "shades_on": "shades_off", "shades_off": "shades_on"}


def _bulk(admin_client, action, lang):
    response = admin_client.post(f"/admin/appearance/mascot?lang={lang}", data={"action": action},
                                 follow_redirects=True)
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.mark.parametrize("lang", sorted(RESULTS))
@pytest.mark.parametrize("action", ["hide", "show", "shades_on", "shades_off"])
def test_bulk_result_counts_one_account_or_many(admin_client, make_user, db, lang, action):
    one, many = RESULTS[lang][action]
    bob = make_user("mascot_count_bob")
    make_user("mascot_count_carol")
    _bulk(admin_client, OPPOSITE[action], lang)
    # Every account changes (the administrator and both members at least)...
    counted = re.search(re.escape(many).replace(r"\{\}", r"(\d+)"), _bulk(admin_client, action, lang))
    assert counted and int(counted.group(1)) >= 3
    # ...then only the one put back.
    key, value = next(iter(service.BULK_ACTIONS[OPPOSITE[action]].items()))
    db.execute("UPDATE users SET accessibility = json_set(accessibility, ?, ?) WHERE id = ?",
               (f"$.{key}", value, bob["id"]))
    html = _bulk(admin_client, action, lang)
    assert one in html
    assert many.format(1) == one or many.format(1) not in html  # Italian "account" has one form


def test_every_action_has_a_singular_and_a_plural_result():
    for lang in BUILTIN_LANGUAGES:
        strings = json.loads((FOLDER / f"{lang}.json").read_text(encoding="utf-8"))
        for action in service.BULK_ACTIONS:
            assert f"mascot.admin.done.{action}_one" in strings
            assert "{count}" in strings[f"mascot.admin.done.{action}_other"]
            assert f"mascot.admin.done.{action}" not in strings


def test_translations_have_the_same_keys():
    english = json.loads((FOLDER / "en.json").read_text(encoding="utf-8"))
    for lang in BUILTIN_LANGUAGES:
        translated = json.loads((FOLDER / f"{lang}.json").read_text(encoding="utf-8"))
        assert english.keys() == translated.keys(), lang
        for key, text in english.items():
            assert set(re.findall(r"\{(\w+)\}", text)) == set(re.findall(r"\{(\w+)\}", translated[key])), (lang, key)


@pytest.mark.parametrize("lang, button, kept, old", [
    ("de", ">Anzeigeeinstellungen zurücksetzen<",
     "Deine Sprache und deine Einstellungen zum Maskottchen bleiben erhalten.", "Alle Anzeigeeinstellungen"),
    ("en", ">Reset display preferences<", "Your language and mascot choices are kept.",
     "Reset all display preferences"),
    ("it", ">Ripristina le preferenze di visualizzazione<", "La lingua e le scelte sulla mascotte restano invariate.",
     "tutte le preferenze di visualizzazione"),
])
def test_reset_says_language_and_mascot_are_kept(client, make_user, login, lang, button, kept, old):
    login(client, make_user("mascot_reset_reader"))
    html = client.get(f"/settings/display?lang={lang}").get_data(as_text=True)
    assert button in html and kept in html
    assert old not in html
