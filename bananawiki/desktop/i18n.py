"""Launcher strings in English and Italian (``translations/<lang>.json``)."""

from __future__ import annotations

import json
import locale
from functools import cache
from pathlib import Path

from . import DesktopError

LANGUAGES = {"en": "English", "it": "Italiano"}
DEFAULT_LANGUAGE = "en"
_DIRECTORY = Path(__file__).resolve().parent / "translations"


@cache
def catalog(language: str) -> dict[str, str]:
    """All strings for *language* (unknown languages get English)."""
    if language not in LANGUAGES:
        language = DEFAULT_LANGUAGE
    return json.loads((_DIRECTORY / f"{language}.json").read_text(encoding="utf-8"))


def system_language() -> str:
    """The operating system's language when the launcher speaks it, else English."""
    try:
        code = locale.getlocale()[0] or ""
    except ValueError:
        code = ""
    code = code.replace("-", "_").split("_")[0].lower()
    if code == "italian":
        code = "it"
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


class Translator:
    def __init__(self, language: str):
        self.language = language if language in LANGUAGES else DEFAULT_LANGUAGE

    def __call__(self, key: str, **params: object) -> str:
        text = catalog(self.language).get(key) or catalog(DEFAULT_LANGUAGE).get(key) or key
        return text.format(**params) if params else text

    def error(self, error: BaseException) -> str:
        """A user-facing explanation of *error*."""
        if isinstance(error, DesktopError):
            message = self(f"error.{error.key}")
            return f"{message}\n\n{error.detail}" if error.detail else message
        return f"{self('error.unexpected')}\n\n{error}"
